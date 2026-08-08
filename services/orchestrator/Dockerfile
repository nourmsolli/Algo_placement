# Dockerfile - orchestrator
FROM python:3.12-slim
WORKDIR /app
COPY services/orchestrator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/ ./shared/
COPY services/orchestrator/ .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
