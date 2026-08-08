# Dockerfile - api_gateway
FROM python:3.12-slim
WORKDIR /app
COPY services/api_gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/ ./shared/
COPY services/api_gateway/ .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
