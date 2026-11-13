FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN addgroup -S kvgroup \
    && adduser -S kvuser -G kvgroup \
    && mkdir -p /data \
    && chown kvuser:kvgroup /data

COPY requirements-fast.txt ./

RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir --only-binary=:all: -r requirements-fast.txt

COPY --chown=kvuser:kvgroup storage.py server.py runtime_logging.py access_logging.py fast_runtime.py fast_http.py consistent_hash.py ./

USER kvuser

EXPOSE 8080

VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import http.client; c = http.client.HTTPConnection('127.0.0.1', 8080, timeout=3); c.request('GET', '/__health_probe__'); r = c.getresponse(); r.read(); c.close(); raise SystemExit(0 if r.status in (200, 404) else 1)"

ENTRYPOINT ["python", "server.py"]

CMD ["--host", "0.0.0.0", "--port", "8080", "--database", "/data/kv.db"]