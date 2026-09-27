FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home opengrid
COPY . .
USER opengrid
CMD ["uvicorn", "opengrid.api:app", "--host", "0.0.0.0", "--port", "8000"]
