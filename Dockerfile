FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/review.db \
    SEED_CSV=/app/data/suppliers.csv \
    SEED_LIMIT=100

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data ./data

RUN mkdir -p /data

EXPOSE 8000

# One uvicorn process per container. docker-compose runs two of these,
# both sharing the SQLite database volume.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
