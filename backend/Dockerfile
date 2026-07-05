FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Overridden by Render (web vs worker) — default to the web service.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
