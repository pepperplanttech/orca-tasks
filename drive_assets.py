"""Read-only access to the shared brand identity folder.

Deliberately NOT domain-wide delegation. This service account has no
delegation and no key -- it is a principal in its own right, and it can see
exactly the Drive folders that have been shared with its email address.
Combined with the drive.readonly scope that gives two independent limits:
read-only, and one folder. Neither depends on an agent behaving well.

Auth resolution:
  * DRIVE_READER_SA_EMAIL set   -> impersonate that SA via IAM Credentials
                                   (local dev; ADC is a human with
                                   roles/iam.serviceAccountTokenCreator)
  * DRIVE_READER_SA_EMAIL unset -> use ADC directly (Cloud Run, where the
                                   service already runs AS the SA)
"""

import io
import os
import sys

import google.auth
from google.auth import impersonated_credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

FOLDER_MIME = "application/vnd.google-apps.folder"

# Google-native formats have no bytes to download; they must be exported.
EXPORT_AS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

# Formats we can read as-is.
PLAIN_TEXT_PREFIXES = ("text/",)
PLAIN_TEXT_EXACT = {"application/json", "application/xml"}


def get_drive_service():
    sa_email = os.environ.get("DRIVE_READER_SA_EMAIL")

    if sa_email:
        # cloud-platform is for the impersonation call itself, not for Drive.
        source, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        creds = impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=sa_email,
            target_scopes=SCOPES,
        )
    else:
        creds, _ = google.auth.default(scopes=SCOPES)

    return build("drive", "v3", credentials=creds)


def _list(service, **kwargs):
    """Paginate files().list, with shared-drive support always on."""
    kwargs.setdefault("fields", "nextPageToken, files(id, name, mimeType, size)")
    kwargs["supportsAllDrives"] = True
    kwargs["includeItemsFromAllDrives"] = True
    # The brand assets live on a shared drive, and the default "user" corpus
    # does not search those -- it silently returns nothing.
    kwargs.setdefault("corpora", "allDrives")
    files, page_token = [], None
    while True:
        resp = service.files().list(pageToken=page_token, **kwargs).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return files


def list_visible(service):
    """Everything this service account can see. Should be the shared folder
    and its contents -- nothing else."""
    return _list(service, q="trashed = false", pageSize=100)


def find_folder(service, name):
    matches = _list(
        service,
        q=f"mimeType = '{FOLDER_MIME}' and name = '{name}' and trashed = false",
    )
    if not matches:
        return None
    if len(matches) > 1:
        raise RuntimeError(
            f"{len(matches)} folders named {name!r} are shared with this "
            "account; set BRAND_ASSETS_FOLDER_ID to disambiguate."
        )
    return matches[0]["id"]


def list_folder(service, folder_id):
    return _list(service, q=f"'{folder_id}' in parents and trashed = false")


def read_file_text(service, file_id, mime_type):
    """Return the file's text, or None if it isn't text at all."""
    export_mime = EXPORT_AS.get(mime_type)
    if export_mime:
        request = service.files().export_media(fileId=file_id, mimeType=export_mime)
    elif mime_type.startswith(PLAIN_TEXT_PREFIXES) or mime_type in PLAIN_TEXT_EXACT:
        request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    else:
        return None

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue().decode("utf-8", errors="replace")


def is_readable(mime_type):
    return (
        mime_type in EXPORT_AS
        or mime_type.startswith(PLAIN_TEXT_PREFIXES)
        or mime_type in PLAIN_TEXT_EXACT
    )


def read_preference(entry):
    """Lower sorts first. A real .md or .json is one direct download that
    keeps its structure; a Google-native file needs a server-side export
    conversion and comes back as flattened text. Prefer the former."""
    return 1 if entry["mimeType"] in EXPORT_AS else 0


def fetch_brand_assets(service, folder_id):
    """Collect the folder's text assets, one per name.

    The folder deliberately holds the same content twice under one name --
    e.g. a Brand_Voice Google Doc and a Brand_Voice.md -- so that Sammy has a
    fallback. Sending both to a campaign agent would hand it two versions of
    the brand voice and no way to know which wins, so this reads the
    preferred form and falls back to the other only if that read fails.

    Returns (documents, skipped, superseded). Binary assets (images, PDFs)
    can't travel in a text payload; they come back in `skipped` rather than
    being silently dropped.
    """
    entries = [
        e for e in list_folder(service, folder_id) if e["mimeType"] != FOLDER_MIME
    ]
    skipped = [e for e in entries if not is_readable(e["mimeType"])]

    groups = {}
    for entry in entries:
        if is_readable(entry["mimeType"]):
            stem = os.path.splitext(entry["name"])[0]
            groups.setdefault(stem, []).append(entry)

    documents, superseded = [], []
    for stem, candidates in groups.items():
        candidates.sort(key=read_preference)
        for index, entry in enumerate(candidates):
            try:
                text = read_file_text(service, entry["id"], entry["mimeType"])
            except HttpError:
                # Fall through to the next candidate; that is the whole point
                # of keeping two copies.
                continue
            if text is None:
                continue
            documents.append({"name": entry["name"], "text": text})
            superseded.extend(candidates[:index] + candidates[index + 1 :])
            break
        else:
            raise RuntimeError(f"No readable copy of {stem!r} in the folder.")

    documents.sort(key=lambda d: d["name"])
    return documents, skipped, superseded


def render_assets_block(documents):
    """Format fetched documents for injection into a routine prompt."""
    parts = []
    for doc in documents:
        parts.append(f"--- {doc['name']} ---\n{doc['text'].strip()}")
    return "\n\n".join(parts)


def main():
    try:
        service = get_drive_service()
    except google.auth.exceptions.DefaultCredentialsError:
        sys.exit("No ADC. Run: gcloud auth application-default login")

    try:
        visible = list_visible(service)
    except HttpError as exc:
        if exc.resp.status == 403:
            sys.exit(
                "Drive returned 403. Either the Drive API isn't enabled on the "
                "project, or ADC lacks roles/iam.serviceAccountTokenCreator on "
                f"{os.environ.get('DRIVE_READER_SA_EMAIL')}."
            )
        raise

    if not visible:
        sys.exit(
            "This service account can see nothing in Drive. Share the brand "
            "identity folder with "
            f"{os.environ.get('DRIVE_READER_SA_EMAIL', '<the SA email>')} "
            "as Viewer."
        )

    print(f"Visible to this service account ({len(visible)} items):\n")
    for entry in visible:
        kind = "folder" if entry["mimeType"] == FOLDER_MIME else entry["mimeType"]
        print(f"  {entry['name']}  [{kind}]  {entry['id']}")

    folder_id = os.environ.get("BRAND_ASSETS_FOLDER_ID")
    if not folder_id:
        name = os.environ.get("BRAND_ASSETS_FOLDER_NAME", "01_Brand_Identity")
        folder_id = find_folder(service, name)
        if not folder_id:
            print(f"\nNo folder named {name!r} among the above.")
            return

    documents, skipped, superseded = fetch_brand_assets(service, folder_id)
    print(f"\nText assets ({len(documents)}):")
    for doc in documents:
        print(f"  {doc['name']}  ({len(doc['text'])} chars)")
    if superseded:
        print(f"\nSuperseded by a preferred copy ({len(superseded)}):")
        for entry in superseded:
            print(f"  {entry['name']}  [{entry['mimeType']}]")
    if skipped:
        print(f"\nNot text, can't be inlined ({len(skipped)}):")
        for entry in skipped:
            print(f"  {entry['name']}  [{entry['mimeType']}]")


if __name__ == "__main__":
    main()
