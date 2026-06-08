FROM python:3.12-slim

WORKDIR /app

# Install only what we need; keeps image small + builds fast on rebuilds
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend

ENV VESTASPOTTER_DATA_DIR=/app/data
ENV PYTHONUNBUFFERED=1

EXPOSE 8011

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8011"]
