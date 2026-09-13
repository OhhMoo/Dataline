FROM python:3.11-slim

ENV PYTHONUTF8=1 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/world

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/world
COPY gateway/requirements.lock /opt/gateway/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /opt/gateway/requirements.lock

COPY gateway/pyproject.toml /opt/gateway/
COPY gateway/src/ /opt/gateway/src/
RUN pip install --no-cache-dir --no-deps /opt/gateway

COPY world/world/ ./world/
COPY world/seed.example.yaml ./seed.yaml
COPY world/requirements.lock ./requirements.lock
COPY world/docker/entrypoint.sh world/docker/egress_test.py ./
COPY gateway/config.example.json /etc/alkera/gateway.json
RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
CMD ["alkera-gateway", "serve", "--config", "/etc/alkera/gateway.json", "--host", "0.0.0.0"]
