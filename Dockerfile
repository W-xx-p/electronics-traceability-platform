FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ELECTROTRACE_DB=/app/data/electrotrace.db \
    ELECTROTRACE_UPLOAD_DIR=/app/data/uploads \
    ELECTROTRACE_OCR_PROVIDER=demo

WORKDIR /app

COPY pyproject.toml README.md ./
COPY backend ./backend
COPY frontend ./frontend
COPY scripts ./scripts

RUN pip install --no-cache-dir . \
    && python scripts/generate_demo_labels.py \
    && groupadd --system electrotrace \
    && useradd --system --gid electrotrace --home-dir /app --shell /usr/sbin/nologin electrotrace \
    && mkdir -p /app/data/uploads \
    && chown -R electrotrace:electrotrace /app/data

USER electrotrace

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
