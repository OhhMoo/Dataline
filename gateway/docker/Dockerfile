FROM python:3.11-slim
ENV PYTHONUTF8=1 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/world
RUN apt-get update && apt-get install -y --no-install-recommends git openssl \
    && rm -rf /var/lib/apt/lists/*
COPY gateway/requirements.lock /opt/gateway/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /opt/gateway/requirements.lock
COPY gateway/pyproject.toml /opt/gateway/
COPY gateway/src /opt/gateway/src
RUN pip install --no-cache-dir --no-deps /opt/gateway
COPY world/world /opt/world/world
COPY world/seed.example.yaml /opt/world/seed.yaml
COPY world/requirements.lock /opt/world/requirements.lock
COPY world/docker/egress_test.py /opt/stack/egress_test.py
COPY gateway/docker /opt/stack
COPY gateway/tests /opt/gateway/tests
# Local Snowflake trust store. Seeded with certifi so REQUESTS_CA_BUNDLE always names a
# valid bundle even when the snowflake profile is off; when it is on, that service
# republishes bundle.pem as certifi + its own certificate over the shared volume.
# Measured 2026-09-13: REQUESTS_CA_BUNDLE is the only trust knob snowflake-connector-python
# 4.7.3 honours - SSL_CERT_FILE never reaches it, and insecure_mode only governs OCSP.
RUN mkdir -p /etc/alkera/snowflake-tls && python -c "import certifi,shutil;shutil.copyfile(certifi.where(),'/etc/alkera/snowflake-tls/bundle.pem')"
ENV REQUESTS_CA_BUNDLE=/etc/alkera/snowflake-tls/bundle.pem
WORKDIR /opt/world
CMD ["python", "/opt/stack/start.py"]
