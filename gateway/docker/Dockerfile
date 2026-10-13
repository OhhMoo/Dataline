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
# ds/da: Playwright's headless Chromium. An analyst drives the Tableau and Sigma web
# UIs with a real Playwright script over the shell (plan decision D5); the audit stays
# the services' own `tableau.ui_*` / `sigma.ui_*` rows. TOOLBELT.md, "Roles".
#   * libraries: what chrome-headless-shell links (readelf NEEDED, both arches,
#     measured 2026-10-13) from the frozen snapshot the toolbelt's apt layer left in
#     /etc/apt, plus fontconfig's config and one font family (Chrome's own
#     fonts-liberation): with no font the browser aborts on the first glyph it lays
#     out (Skia "Not implemented", measured). libgbm1 is most of the layer's apt
#     cost - it hard-depends on mesa-libgallium and libllvm19 - for a library the
#     shell links and never renders with (it ships SwiftShader).
#   * the browser: what Playwright 1.63.0 (the role lock) pins - chromium_headless_
#     shell-1243 = Chrome for Testing 153.0.8010.12, and the ffmpeg-1011 it installs
#     with it - fetched by `playwright install` while the build has the network, into
#     a fixed /opt/ms-playwright, and checked by sha256 per arch, so a CDN serving
#     other bytes fails the build.
#   * found by Playwright's DEFAULT lookup, ~/.cache/ms-playwright, linked for root
#     (the agent here): no PLAYWRIGHT_BROWSERS_PATH in the box's environment. An ENV
#     cannot be set for ds/da alone in this one-stage file, and a de box - every
#     unprofiled episode - must not gain one.
#   * launched once on about:blank, so a browser that cannot start fails the build;
#     toolbelt_check.py's CHROMIUM_PROBE proves it again in the image as built.
RUN set -eu; \
    case "$ALKERA_TOOLBELT" in \
      ds|da) \
        apt-get update; \
        apt-get install -y --no-install-recommends \
          fontconfig-config=2.15.0-2.3 \
          fonts-liberation=1:2.1.5-3 \
          libasound2t64=1.2.14-1 \
          libatk-bridge2.0-0t64=2.56.2-1+deb13u1 \
          libatk1.0-0t64=2.56.2-1+deb13u1 \
          libatspi2.0-0t64=2.56.2-1+deb13u1 \
          libdbus-1-3=1.16.2-2 \
          libgbm1=25.0.7-2+deb13u1 \
          libglib2.0-0t64=2.84.4-3~deb13u3 \
          libnspr4=2:4.36-1 \
          libnss3=2:3.110-1+deb13u4 \
          libx11-6=2:1.8.12-1 \
          libxcb1=1.17.0-2+b1 \
          libxcomposite1=1:0.4.6-1 \
          libxdamage1=1:1.1.6-1+b2 \
          libxext6=2:1.3.4-1+b3 \
          libxfixes3=1:6.0.0-2+b4 \
          libxkbcommon0=1.7.0-2 \
          libxrandr2=2:1.5.4-1+b3 \
        ; \
        rm -rf /var/lib/apt/lists/*; \
        PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright python -m playwright install --only-shell chromium; \
        case "$(dpkg --print-architecture)" in \
          amd64) shell=chrome-headless-shell-linux64; \
                 shell_sum=ded93a9c9a53a1ae040f08124badcca95c938e9d5015ff340c3b5538c41bf39e; \
                 ffmpeg_sum=460d44f3416005662f528d4b92e7b94ace924e8a0288106d3803b73c56eaadc8 ;; \
          arm64) shell=chrome-headless-shell-linux-arm64; \
                 shell_sum=f5d89353cc9ef8dc1541268bbee1f05ee40a31ce3d9799b3a274e5147f6a8cdb; \
                 ffmpeg_sum=b662355fff37fa4a0c2a353b26b3255f16a5e7fd358d97231e5a5eedd511158c ;; \
          *) echo "no pinned chromium-headless-shell for this architecture" >&2; exit 1 ;; \
        esac; \
        printf '%s  %s\n' \
          "$shell_sum" "/opt/ms-playwright/chromium_headless_shell-1243/$shell/chrome-headless-shell" \
          "$ffmpeg_sum" /opt/ms-playwright/ffmpeg-1011/ffmpeg-linux | sha256sum -c -; \
        mkdir -p /root/.cache && ln -s /opt/ms-playwright /root/.cache/ms-playwright; \
        python -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); browser = p.chromium.launch(); page = browser.new_page(); page.goto('about:blank'); print('chromium', browser.version, page.url); browser.close(); p.stop()" ;; \
      *) echo "toolbelt role $ALKERA_TOOLBELT: no browser" ;; \
    esac
# Warehouse clients beyond DuckDB/Postgres, opt-in per image, installed AFTER the
# role so nothing later can undo their one divergence. ALKERA_WAREHOUSE_CLIENTS is
# a comma-separated list of client sets (`clients` in alkera_gateway.backends;
# build_episode.py --warehouse-clients), and each `c` in it installs
# requirements-$c.lock. Empty (the default) installs nothing, so the default
# image's gateway environment is exactly the locks above. "snowflake" is
# requirements-snowflake.lock (snowflake-connector-python and dbt-snowflake).
# A client lock is compiled by docker/lock_warehouse_clients.py <c>, NOT exported
# from uv.lock: dbt-snowflake caps certifi below 2025.4.26, and resolving it with
# the gateway would hold back every image's CA bundle. It is constrained to
# requirements.lock, -native and -ds, so every package it shares with them is the
# same version and pip reports it satisfied - except certifi, which a
# snowflake-client image (and only that image) takes at the capped version
# (docker/snowflake-clients.overrides).
#   gateway/.venv/bin/python gateway/docker/lock_warehouse_clients.py <c>
# The client sets that exist are the backend registry's (alkera_gateway/backends.py,
# read with the stdlib by docker/warehouse_clients.py, before the gateway is
# installed): a name it does not list, or a set with no lock, fails the build
# rather than quietly installing nothing or a role's lock. Every lock is staged
# and only the requested ones stay, at /opt/gateway/requirements-$c.lock.
# `set -f`: a client name is a word, never a glob.
ARG ALKERA_WAREHOUSE_CLIENTS=""
COPY gateway/docker/warehouse_clients.py /opt/gateway/registry/warehouse_clients.py
COPY gateway/src/alkera_gateway/plugs.py gateway/src/alkera_gateway/backends.py /opt/gateway/registry/alkera_gateway/
COPY gateway/requirements-*.lock /opt/gateway/clients/
RUN set -euf; \
    known=" $(python /opt/gateway/registry/warehouse_clients.py /opt/gateway/registry/alkera_gateway) "; \
    for c in $(echo "$ALKERA_WAREHOUSE_CLIENTS" | tr ',' ' '); do \
      case "$known" in *" $c "*) ;; *) echo "'$c' is not a warehouse client set the backend registry names:$known" >&2; exit 1 ;; esac; \
      lock="/opt/gateway/clients/requirements-$c.lock"; \
      [ -f "$lock" ] || { echo "no gateway/requirements-$c.lock for warehouse clients '$c'" >&2; exit 1; }; \
      pip install --no-cache-dir --retries 5 --timeout 120 --require-hashes -r "$lock"; \
      cp "$lock" "/opt/gateway/requirements-$c.lock"; \
    done; \
    [ -n "$ALKERA_WAREHOUSE_CLIENTS" ] || echo "warehouse clients: none beyond requirements.lock"; \
    rm -rf /opt/gateway/clients /opt/gateway/registry
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
# `jupyter kernelspec list` names it in the image as built, under `alkera-python`
# and over ipykernel's own `python3` (the name nbclient and `jupyter execute`
# default to), each checked byte-equal to the checked-in copy that
# docker/jupyter/launch.py (run from /opt/stack/jupyter by the plug's own
# container) serves. /opt/stack is stripped from the EPISODE container only
# (tasks/harness/container_episode.py STRIP_PATHS, exec'd into that one
# container), so the jupyter container keeps its launcher.
RUN case "$ALKERA_TOOLBELT" in \
      ds|da) python -c "from alkera_gateway.native.notebook import install_audited_kernelspecs; install_audited_kernelspecs('/usr/local/share/jupyter', interpreter='/usr/local/bin/python')" \
             && cmp /usr/local/share/jupyter/kernels/alkera-python/kernel.json /opt/stack/jupyter/kernels/alkera-python/kernel.json \
             && cmp /usr/local/share/jupyter/kernels/python3/kernel.json /opt/stack/jupyter/kernels/alkera-python/kernel.json ;; \
    esac
COPY gateway/tests /opt/gateway/tests
# Local Snowflake trust store. Seeded with certifi so REQUESTS_CA_BUNDLE always names a
# valid bundle even when the snowflake profile is off; when it is on, that service
# republishes bundle.pem as certifi + its own certificate over the shared volume.
# Measured 2026-09-13: REQUESTS_CA_BUNDLE is the only trust knob snowflake-connector-python
# 4.7.3 honours - SSL_CERT_FILE never reaches it, and insecure_mode only governs OCSP.
RUN mkdir -p /etc/alkera/snowflake-tls && python -c "import certifi,shutil;shutil.copyfile(certifi.where(),'/etc/alkera/snowflake-tls/bundle.pem')"
ENV REQUESTS_CA_BUNDLE=/etc/alkera/snowflake-tls/bundle.pem
# The repository is a BIND MOUNT (/workspace, owned by the operator's uid on the
# host) and the agent is root in here, so git >= 2.35.2 refuses it outright:
# `fatal: detected dubious ownership in repository at '/workspace'` on every
# `git status`, measured from inside the episode container. That is not what a
# data engineer's box does - a dev container with a mounted checkout marks it
# safe, which is what VS Code's devcontainers do for their workspace folder.
# --system (/etc/gitconfig), not --global: /root is the agent's home and the
# harness copies the world's `home/` over it, so a ~/.gitconfig could be
# replaced; the system file is the image's. Exactly one path - not `*`, which
# would also silence the check for anything else the agent clones or mounts.
RUN git config --system --add safe.directory /workspace
WORKDIR /opt/world
CMD ["python", "/opt/stack/start.py"]
