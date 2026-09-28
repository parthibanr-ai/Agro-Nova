# Cloud Run image. Build: gcloud builds submit --tag REGION-docker.pkg.dev/PROJECT/agrin/api
#
# Database schema changes are applied as a separate release step, before new instances start:
#   python -m app.migrate        (for example as a Cloud Run job using this same image)
# and instances run with AUTO_MIGRATE=false (the default for Postgres). See docs/env_reference.md.
FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini .
COPY scripts ./scripts
# Cloud Run injects $PORT. Set WEB_CONCURRENCY=N to run N worker processes per container (uvicorn reads it).
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
