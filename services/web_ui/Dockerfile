# Dockerfile - web_ui
FROM python:3.12-slim
WORKDIR /app
COPY services/web_ui/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared/ ./shared/
COPY services/web_ui/ .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
