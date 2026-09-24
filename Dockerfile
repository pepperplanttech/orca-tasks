FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# Runs one tick: reap stale tasks, then fire the next queued one if nothing
# is in flight. Cloud Scheduler invokes this as a Cloud Run job.
ENTRYPOINT ["python", "orchestrator.py"]
