# Cloud Run image. Build: gcloud builds submit --tag REGION-docker.pkg.dev/PROJECT/agrin/api
FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# Cloud Run injects $PORT
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}
