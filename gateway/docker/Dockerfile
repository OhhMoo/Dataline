# The episode image. Everything the agent can touch during a rollout is in here:
# the world, the gateway, and - since 2026-09-14 - a toolbelt instead of two binaries.
#
# Base pinned by digest, not by tag. `python:3.11-slim` moves; a rollout whose image
# was built from a different `3.11-slim` is a different measurement wearing the same
# name. Resolve a new digest with `docker buildx imagetools inspect python:3.11-slim`.
# The digest is a multi-arch OCI index (linux/amd64 and linux/arm64/v8 among eight),
# checked against registry-1.docker.io on 2026-10-02, so `--platform linux/arm64`
# resolves the same pin to its arm64 manifest. See TOOLBELT.md, Determinism.
FROM python@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

# The revision this image was built from, stamped so the running container can be
# asked what source it carries. gateway/docker/build_episode.py supplies it from a
# clean worktree; gateway/tests/test_image_parity.py fails when it drifts from HEAD.
ARG ALKERA_SOURCE_REV=""
# Non-empty when the build carried files that are not in that revision. Only
# gateway/docker/** may be overlaid, and the value is the sha256 of their contents,
# so "this image is HEAD" and "this image is HEAD plus a pending docker change" can
# never be confused for one another after the fact.
ARG ALKERA_SOURCE_OVERLAY=""
LABEL org.alkera.source.revision=$ALKERA_SOURCE_REV
LABEL org.alkera.source.overlay=$ALKERA_SOURCE_OVERLAY
ENV ALKERA_SOURCE_REV=$ALKERA_SOURCE_REV

ENV PYTHONUTF8=1 TZ=UTC PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/world

# --- the toolbelt -----------------------------------------------------------------
# Until 2026-09-14 this line installed `git openssl` and nothing else, while
# tasks/harness/container_episode.py drew a diagram promising the agent psql. The
# northstar is a data engineer's working computer; a box with two binaries is a stub
# that tells the agent it is in a sandbox the first time it types `less`.
#
# Determinism: apt is pointed at a snapshot.debian.org timestamp, so `apt-get install`
# resolves against a frozen archive rather than whatever deb.debian.org serves today -
# the same reason requirements.lock exists. Top-level versions below are the versions
# that snapshot holds; dependencies are pinned implicitly by the snapshot itself. A
# version that no longer resolves is a loud build failure, which is the intent.
# Refresh: bump ALKERA_DEBIAN_SNAPSHOT, re-run `apt-cache policy` for each package.
#
# Rejected on purpose - see gateway/docker/TOOLBELT.md for the full ledger.
ARG ALKERA_DEBIAN_SNAPSHOT=20260910T000000Z
RUN set -eux; \
    printf '%s\n' \
      'Types: deb' \
      "URIs: http://snapshot.debian.org/archive/debian/${ALKERA_DEBIAN_SNAPSHOT}/" \
      'Suites: trixie trixie-updates' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
      '' \
      'Types: deb' \
      "URIs: http://snapshot.debian.org/archive/debian-security/${ALKERA_DEBIAN_SNAPSHOT}/" \
      'Suites: trixie-security' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
      > /etc/apt/sources.list.d/debian.sources; \
    echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99snapshot; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      git=1:2.47.3-0+deb13u1 \
      openssl=3.5.7-1~deb13u2 \
      ca-certificates=20250419 \
      curl=8.14.1-2+deb13u4 \
      jq=1.7.1-6+deb13u3 \
      less=668-1 \
      make=4.4.1-2 \
      unzip=6.0-29+deb13u1 \
      procps=2:4.0.4-9 \
      tree=2.2.1-1 \
      file=1:5.46-5 \
      vim=2:9.1.1230-2 \
      nano=8.4-1+deb13u1 \
      netcat-openbsd=1.229-1 \
      bind9-dnsutils=1:9.20.26-1~deb13u1 \
      iputils-ping=3:20240905-3 \
      openssh-client=1:10.0p1-7+deb13u4 \
      postgresql-client-17=17.11-0+deb13u1 \
    ; \
    rm -rf /var/lib/apt/lists/*

# Python CLIs a data engineer has on PATH, in their own virtualenv. They are NOT
# installed into the gateway's environment: awscli pins botocore, the gateway pins
# boto3, and one resolver deciding that argument for both is how a toolbelt addition
# silently changes what `s3_put` does. /opt/toolbelt/bin is APPENDED to PATH so the
# venv's `python` can never shadow /usr/local/bin/python.
#
# Same venv for every role, one lock per arch (toolbelt.lock / toolbelt.arm64.lock;
# the two pin identical versions and differ only in their header). The arch is read
# from dpkg inside the image, not from TARGETARCH: the legacy builder never sets
# TARGETARCH, and the image's own architecture is the fact the wheels must match.
# Role LIBRARIES (pandas, ...) are not here - see ALKERA_TOOLBELT below.
COPY gateway/docker/toolbelt.lock gateway/docker/toolbelt.arm64.lock /opt/toolbelt-locks/
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) lock=toolbelt.lock ;; \
      arm64) lock=toolbelt.arm64.lock ;; \
      *) echo "no toolbelt lock for architecture $arch" >&2; exit 1 ;; \
    esac; \
    mv "/opt/toolbelt-locks/$lock" /opt/toolbelt.lock; \
    rm -rf /opt/toolbelt-locks; \
    python -m venv /opt/toolbelt; \
    /opt/toolbelt/bin/pip install --no-cache-dir --retries 5 --timeout 120 --require-hashes -r /opt/toolbelt.lock; \
    find /opt/toolbelt -name '__pycache__' -prune -exec rm -rf {} +
ENV PATH=$PATH:/opt/toolbelt/bin

# A data engineer's box has ~/.aws filled in; an `aws` that answers NoCredentialsError
# on the first command is a tool the agent cannot use and a tell that the box is fake.
# These are the same credentials, endpoint and addressing style storage.py hands boto3
# (aws_access_key_id="episode", http://s3:9090, path addressing), so `aws s3 ls` and
# `s3_list` see one object store. A world with no S3 service answers with a connection
# error, which is what a misconfigured endpoint looks like anywhere else.
# `aws-keys` and not `aws-credentials`: the repository's .gitignore line 6 is
# `*credentials*`, so a file with that name silently never reaches a commit - and the
# episode image is built from a clean worktree, where the COPY would then fail.
COPY gateway/docker/aws-config /root/.aws/config
COPY gateway/docker/aws-keys /root/.aws/credentials
RUN chmod 600 /root/.aws/credentials

COPY gateway/requirements.lock /opt/gateway/requirements.lock
RUN pip install --no-cache-dir --retries 5 --timeout 120 --require-hashes -r /opt/gateway/requirements.lock
# The `native` extra (gateway/NATIVE.md): fastapi/uvicorn for the nine
# fake vendor services and the one process that mounts them, httpx for the
# tailers, and the REAL vendor clients (jira, slack_sdk, atlassian-python-api)
# their compatibility tests are driven by. Until this line existed the service
# modules were in the image and none of them could be imported - measured,
# `services.load()` returned nine `No module named 'fastapi'`, which is why
# containment P6 had to be measured against `--stub`ped services
# (docs/implementation_plan/gateway/COMPOSE_REQUESTS.md item 1).
#
# A second --require-hashes lock and not `pip install '.[native]'`: this
# repository installs from locks, and both locks are exported from the one
# gateway/uv.lock, so a package in both carries the same version in both by
# construction. Refresh with
#   uv export --locked --extra native --no-dev --no-emit-project \
#     --format requirements-txt --output-file requirements-native.lock
COPY gateway/requirements-native.lock /opt/gateway/requirements-native.lock
RUN pip install --no-cache-dir --retries 5 --timeout 120 --require-hashes -r /opt/gateway/requirements-native.lock
# --- the role ---------------------------------------------------------------------
# Environment profiles are <role>/<stack>, roles de/ds/da (alkera_gateway.plugs).
# A tool a role does not get is NOT INSTALLED - never installed and masked off PATH,
# which `pip list` sees through in one command. de (the default) adds nothing, so
# the default image is the pre-roles image.
#
# ds and da install their libraries into THIS interpreter, not /opt/toolbelt: they
# are what the agent's own `python` imports, and what the Jupyter plug's kernel
# (docker/jupyter/kernels/alkera-python, argv /usr/local/bin/python) imports next to
# alkera_gateway.native.notebook. The reason awscli lives in a venv - one resolver
# arguing botocore for two masters - does not apply: both locks are exports of the
# one gateway/uv.lock, which resolved the notebook and ds extras together with the
# gateway's own pins, so every package they share with the locks above has the same
# version and pip reports it already satisfied.
#   ds  requirements-ds.lock  notebook + ds extras: pandas, pyarrow, polars,
#                             scikit-learn, matplotlib, ipykernel, jupyter-server, ...
#   da  requirements-da.lock  the same export with polars and scikit-learn pruned
#                             (and scipy, joblib, ... that only they needed)
# Refresh (gateway/):
#   uv export --locked --extra notebook --extra ds --no-dev --no-emit-project \
#     --format requirements-txt --output-file requirements-ds.lock
#   uv export --locked --extra notebook --extra ds --prune polars --prune scikit-learn \
#     --no-dev --no-emit-project --format requirements-txt --output-file requirements-da.lock
ARG ALKERA_TOOLBELT=de
COPY gateway/requirements-ds.lock /opt/gateway/requirements-ds.lock
COPY gateway/requirements-da.lock /opt/gateway/requirements-da.lock
RUN set -eu; \
    case "$ALKERA_TOOLBELT" in \
      de) echo "toolbelt role de: no libraries beyond the gateway's" ;; \
      ds|da) pip install --no-cache-dir --retries 5 --timeout 120 --require-hashes \
               -r "/opt/gateway/requirements-$ALKERA_TOOLBELT.lock" ;; \
      *) echo "ALKERA_TOOLBELT must be de, ds or da, not '$ALKERA_TOOLBELT'" >&2; exit 1 ;; \
    esac; \
    for r in ds da; do [ "$r" = "$ALKERA_TOOLBELT" ] || rm -f "/opt/gateway/requirements-$r.lock"; done; \
    printf '%s\n' "$ALKERA_TOOLBELT" > /opt/toolbelt/ROLE
LABEL org.alkera.toolbelt=$ALKERA_TOOLBELT
ENV ALKERA_TOOLBELT=$ALKERA_TOOLBELT
# Warehouse clients beyond DuckDB/Postgres, opt-in per image, installed AFTER the
# role so nothing later can undo their one divergence. Empty (the default)
# installs nothing, so the default image's gateway environment is exactly the
# locks above. Containing "snowflake" adds requirements-snowflake.lock
# (snowflake-connector-python and dbt-snowflake). That lock is compiled by
# docker/lock_snowflake_clients.py, NOT exported from uv.lock: dbt-snowflake caps
# certifi below 2025.4.26, and resolving it with the gateway would hold back every
# image's CA bundle. It is constrained to requirements.lock, -native and -ds, so
# every package it shares with them is the same version and pip reports it
# satisfied - except certifi, which a snowflake-client image (and only that image)
# takes at the capped version (docker/snowflake-clients.overrides).
#   gateway/.venv/bin/python gateway/docker/lock_snowflake_clients.py
ARG ALKERA_WAREHOUSE_CLIENTS=""
COPY gateway/requirements-snowflake.lock /opt/gateway/requirements-snowflake.lock
RUN case ",$ALKERA_WAREHOUSE_CLIENTS," in \
      *snowflake*) pip install --no-cache-dir --retries 5 --timeout 120 --require-hashes -r /opt/gateway/requirements-snowflake.lock ;; \
      *) echo "warehouse clients: none beyond requirements.lock" ;; \
    esac
LABEL org.alkera.warehouse_clients=$ALKERA_WAREHOUSE_CLIENTS
ENV ALKERA_WAREHOUSE_CLIENTS=$ALKERA_WAREHOUSE_CLIENTS
COPY gateway/pyproject.toml /opt/gateway/
COPY gateway/src /opt/gateway/src
RUN pip install --no-cache-dir --retries 5 --timeout 120 --no-deps /opt/gateway
# The DuckDB `postgres` extension, baked in. A dual-warehouse world with a DuckDB
# primary and a Postgres peer attaches the peer into the primary's catalog
# (gateway/src/alkera_gateway/warehouses.py, AttachedDuckDBWarehouse) and needs
# `LOAD postgres`; `INSTALL` fetches from extensions.duckdb.org, and the episode
# zone has no egress (evidence/toolbelt.json: curl -> "Could not resolve host"),
# so it cannot happen at run time. Installed here, where the build has the
# network, into the interpreter's own extension directory (~/.duckdb, root's, the
# user every process in this image runs as), for the exact duckdb the lock pins.
# The gateway opens connections with autoinstall/autoload OFF, so the extension
# is only ever loaded by an explicit LOAD before the connection is locked down.
# toolbelt_check.py records its presence and sha256 in evidence/image.json.
RUN python -c "import duckdb; c = duckdb.connect(); c.execute('INSTALL postgres'); c.execute('LOAD postgres'); rows = c.execute(\"select extension_name, extension_version, install_path from duckdb_extensions() where loaded and (extension_name = 'postgres' or list_contains(aliases, 'postgres'))\").fetchall(); print(rows); assert rows, 'postgres extension did not load'"
COPY world/world /opt/world/world
COPY world/seed.example.yaml /opt/world/seed.yaml
COPY world/requirements.lock /opt/world/requirements.lock
COPY world/docker/egress_test.py /opt/stack/egress_test.py
COPY gateway/docker /opt/stack
# ds/da: the Jupyter plug's audited kernelspec, installed system-wide so
# `jupyter kernelspec list` names it in the image as built, and checked byte-equal
# to the checked-in copy that docker/jupyter/launch.py (run from /opt/stack/jupyter
# by the plug's own container) serves. /opt/stack is stripped from the EPISODE
# container only (tasks/harness/container_episode.py STRIP_PATHS, exec'd into that
# one container), so the jupyter container keeps its launcher.
RUN case "$ALKERA_TOOLBELT" in \
      ds|da) python -c "from alkera_gateway.native.notebook import install_kernelspec; install_kernelspec('/usr/local/share/jupyter', interpreter='/usr/local/bin/python')" \
             && cmp /usr/local/share/jupyter/kernels/alkera-python/kernel.json /opt/stack/jupyter/kernels/alkera-python/kernel.json ;; \
    esac
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
