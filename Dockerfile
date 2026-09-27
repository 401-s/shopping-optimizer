FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shopping_optimizer.py .

# Draai als nobody:users (99:100), de standaard eigenaar van Unraid appdata
RUN useradd --uid 99 --gid 100 --no-create-home --shell /usr/sbin/nologin optimizer \
    && mkdir -p /app/data && chown 99:100 /app/data
USER 99:100

EXPOSE 8099

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\", \"8099\")}/health', timeout=4)"

CMD ["python", "shopping_optimizer.py"]
