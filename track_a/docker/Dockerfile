FROM python:3.11-slim

ENV PYTHONUTF8=1 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/world
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY world/ ./world/
COPY seed.example.yaml ./seed.yaml
COPY docker/entrypoint.sh docker/egress_test.py ./
RUN chmod +x entrypoint.sh

# Boot: restore /snapshot/world.tar if mounted, else build from seed, then run
# CMD. Episode CMD is a placeholder — Track B replaces it with the tool gateway.
ENTRYPOINT ["./entrypoint.sh"]
CMD ["sleep", "infinity"]
