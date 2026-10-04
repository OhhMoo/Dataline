# The episode toolbelt — what the box ships, and what it does not

The northstar is that the sandbox mocks a real data engineer's working computer as it
actually is. Until 2026-09-14 the Dockerfile's only `apt-get install` line read
`git openssl`, while `tasks/harness/container_episode.py` drew an architecture diagram
promising the agent "bash, git, dbt, psql, python". `psql` was not installed. This file
is the ledger so that cannot drift again quietly: every binary is here with the reason it
earned its place, and every rejection is here with the reason it did not.

The bar for an addition is both of:

1. **Would a data engineer reach for this?** and
2. **Does having it change what an agent can plausibly do in one of our worlds?**

"Both", not "either" — otherwise the image becomes a Debian mirror. The one standing
exception is tools whose *absence* is itself a tell (see `vim`, `ssh`).

---

## Installed, and why

### apt, from a pinned Debian snapshot

| package | binaries | why it earns its place |
|---|---|---|
| `git` | `git` | the workspace is a real git repo; pre-existing |
| `openssl` | `openssl` | pre-existing; hashes, certs, the Snowflake trust store |
| `ca-certificates` | — | a trust store `curl`, `aws` and the TLS route all need |
| `curl` | `curl` | the gateway speaks HTTP on 127.0.0.1:8080; the S3 mock speaks HTTP. A DE curls an endpoint before believing anything about it |
| `jq` | `jq` | the action log is JSONL, dbt's artifacts are JSON, `aws` emits JSON. Reading those without `jq` is not how anyone does it |
| `less` | `less` | `git log` and `git diff` page through `$PAGER`; without it git degrades and the box reads as stripped |
| `make` | `make` | dbt repos and DE repos ship Makefiles; `make run` is a first move |
| `unzip` | `unzip` | **load-bearing.** `excel_close` writes real `.xlsx` workbooks into the workspace and neither lock ships a reader. `unzip -p BOOK.xlsx xl/worksheets/sheet1.xml` is a route to that data |
| `procps` | `ps`, `top` | diagnosing a job that is still running, or is not. The scheduler executes real dbt now |
| `tree` | `tree` | surveying an unfamiliar repo; one command instead of twelve `ls` |
| `file` | `file` | a vendor drop whose extension lies is a standing DE pathology; `file` is how you find out |
| `vim`, `nano` | `vim`, `vi`, `nano` | see "absence as a tell" below |
| `netcat-openbsd` | `nc` | "is the backend up or is my DSN wrong" — `nc -z postgres 5432` separates the two |
| `bind9-dnsutils` | `dig`, `nslookup` | same question one layer down; service-name resolution is the thing that actually breaks in a compose topology |
| `iputils-ping` | `ping` | first reflex, and here it returns a *true* negative about egress rather than "command not found" |
| `openssh-client` | `ssh`, `sftp`, `ssh-keygen` | the `sftp_vendor` theme's own seeded fault is `paramiko.ssh_exception.SSHException: Connection reset by peer`. A box where `sftp` is not a command breaks the frame in exactly the theme that most needs it |
| `postgresql-client-17` | `psql`, `pg_dump`, `pg_isready` | the diagram promised `psql`; `warehouse_backend: postgres` is a real, tested backend and `postgres` is a real service on `backend_net`. Major 17 is chosen against the pinned server (`PG_MAJOR=16`): a newer `pg_dump` reads an older server, an older one refuses |

### pip, in an isolated virtualenv at `/opt/toolbelt`

`gateway/docker/toolbelt.in` → `toolbelt.lock`, compiled with
`uv pip compile --generate-hashes` and installed with `--require-hashes`, exactly as
`snowflake.in`/`snowflake.lock` already do. Every role gets this same venv; its arm64
twin is `toolbelt.arm64.lock`. The ds/da *libraries* are deliberately not here (see
"Roles" below).

They are **not** installed into the gateway's own environment. `awscli` pins `botocore`,
the gateway pins `boto3`, and letting one resolver settle that argument for both is how a
toolbelt addition silently changes what `s3_put` does. `/opt/toolbelt/bin` is **appended**
to `PATH`, never prepended, so the venv's `python` can never shadow
`/usr/local/bin/python` — `test_image_parity.py` asserts that from inside the container.

| package | binaries | why |
|---|---|---|
| `awscli==1.46.1` | `aws` | there is a real S3 service at `s3:9090` on `backend_net`, `boto3` is already in the gateway lock, and `s3_*` tools exist. An agent that can `aws s3 ls` is doing what a DE does. `/root/.aws` is seeded with the same credentials, endpoint and path addressing `storage.py` hands `boto3`, so the CLI and the tool surface see one object store — an `aws` that answers `NoCredentialsError` is not a tool |
| `sqlfluff==4.3.0` | `sqlfluff` | the world ships a real dbt project; linting SQL before running it is standard practice in a dbt shop, and it is a *cheap* action an agent can take, which matters for process signals |
| `tabcmd==2.0.21`, `tableauserverclient==0.41` | `tabcmd`; `import tableauserverclient` | the Tableau plug (`tableau.internal`, a fake Tableau Server the real clients are tested against): a Tableau shop publishes `.twb`/`.tds` with `tabcmd publish`, refreshes extracts with `tabcmd refreshextracts` and exports view data with `tabcmd export --csv`, or scripts the same through TSC. Installed for every role because the CLI is inert without the plug: no `tableau.internal` resolves unless a profile plugs Tableau. Both MIT |
| `csvkit==2.2.0` | `csvstat`, `csvlook`, `in2csv`, `csvsql`, `csvcut`, `csvgrep`, … | the landing zones are CSV (`revenue_daily`, `sftp_vendor`, `mainframe_batch`). `csvstat` profiles a partition without loading it into the warehouse first. It also pulls `openpyxl` and gives `in2csv` — the only shipped way to read `excel_close`'s `.xlsx` as tabular data |

### Absence as a tell

`vim` and `openssh-client` fail bar 2 on a literal reading: there is no interactive
terminal a `vim` session would be useful in, and both compose networks are
`internal: true` so `ssh` can reach nothing. They are installed anyway, because
`command not found` for a binary that exists on every engineer's machine is a signal to
the agent that the box is a fixture. `vim-tiny` was rejected in favour of `vim`: it
registers `vi` and `vim.tiny` but not `vim`, which is the exact tell being avoided, at a
measured cost of 44 MB for `/usr/share/vim`.

### Already in the base image — verified, not installed

`bash`, `sh`, `tar`, `gzip`, `find`, `grep`, `sed`, `awk` (mawk), `diff`, and coreutils
(`ls`, `cat`, `sort`, `uniq`, `wc`, `cut`, `xargs`, `df`, `du`, …) ship with
`python:3.11-slim`. `coreutils` is `Essential: yes` in Debian, so installing it is a
no-op; `toolbelt_check.py` executes each of these anyway rather than assuming, because
"present in the base image" is a claim about a base image that is now digest-pinned but
was not before.

---

## Rejected, and why

| candidate | verdict |
|---|---|
| `duckdb` CLI | **Rejected, and this one is a judgement call worth re-taking.** The warehouse *is* DuckDB, so a DE would absolutely have it. But contract §9.2.1 is that the warehouse is reached through `sql_*`, and shipping the CLI makes off-channel mutation the path of least resistance: `op_class`, `rows_affected` and the SQL text stop reaching the action log for any work done that way. `tasks/harness/README.md` already documents that cost as paid knowingly for `python` + `duckdb`; making it *ergonomic* is a measurement decision that belongs to the track that owns the measurement, not to the image |
| `htop` | Rejected. Interactive TUI, no terminal to be interactive in; `procps` gives `ps`/`top` for the same question |
| `rsync` | Rejected. Nothing to sync to — no second host is reachable — and `cp`/`tar` cover local copies |
| `sqlite3` | Rejected on bar 2. Nothing in any of the four worlds is a SQLite database |
| `wget` | Rejected as redundant with `curl` |
| `xz-utils`, `bzip2`, `zstd` | Rejected on bar 2. Nothing in any world is compressed with them; `tar` and `gzip` are present |
| `dos2unix` | Rejected. Sounds theme-relevant, but grepping the four theme generators found no CRLF and no EBCDIC — an addition justified by a pathology we do not actually plant |
| `zip` | Rejected. Nothing requires *producing* an archive; `unzip` is the direction the worlds need |
| `patch`, `gawk`, `column`, `gettext-base` | Rejected as redundant with `git apply`, `mawk`, and plain shell |
| AWS CLI **v2** | Rejected in favour of v1. v2 ships as a ~60 MB zip with no published sha256 alongside it (GPG `.sig` only), which does not pin the way the rest of this repo pins. v1 is on PyPI and goes straight into `toolbelt.lock` with hashes |
| `openpyxl`/`pandas` in the *gateway* environment of a **de** image | Not done. `openpyxl` arrives inside `/opt/toolbelt` via `csvkit`, which gives `in2csv`. ds and da images *do* put pandas in the system interpreter ("Roles"), from the same `uv.lock` the gateway resolves; a de image does not, so a de box's tools behave exactly as before roles existed |


## Roles: de, ds, da

Environment profiles are `<role>/<stack>` (`alkera_gateway.plugs.ROLES`). The role picks
the box **by what is installed**: a tool a role does not get is not in its image.
Masking it off `PATH` was rejected outright - `pip list` sees through a mask in one
command, and then the box has told the agent it is a fixture *and* which fixture.

| role | system interpreter gets | lock | build |
|---|---|---|---|
| de | the gateway only (`requirements.lock` + `requirements-native.lock`) | — | default; `alkera-episode:head` |
| ds | + `notebook` and `ds` extras | `gateway/requirements-ds.lock` | `--toolbelt ds` → `alkera-episode:head-ds` |
| da | + the same, minus polars and scikit-learn | `gateway/requirements-da.lock` | `--toolbelt da` → `alkera-episode:head-da` |

**Why the system interpreter, when awscli gets a venv.** The libraries are what the
agent's own `python` imports - `/usr/local/bin/python`, which the shadowing rule above
keeps first on `PATH` - and what the Jupyter plug's kernel imports: its kernelspec
(`docker/jupyter/kernels/alkera-python/kernel.json`) runs `/usr/local/bin/python -m
ipykernel_launcher` with `alkera_gateway.native.notebook` preloaded, so pandas, ipykernel
and the gateway must share one interpreter. In `/opt/toolbelt` they would be invisible to
both. The reason the venv exists does not carry over: awscli is resolved by a second
resolver against the gateway's boto3, while both role locks are `uv export`s of the one
`gateway/uv.lock`, which resolved the `notebook` and `ds` extras *together with* the
gateway's own pins. Every package a role lock shares with `requirements.lock` or
`requirements-native.lock` therefore carries the same version (asserted by
`test_toolbelt_roles.py`), and pip, run after them, reports it already satisfied: a role
adds packages and never moves one. Measured on x86_64 with `pip install
--require-hashes` in exactly the Dockerfile's order: `pip check` clean for all three.

**da is its own export, not ds's lock reused.** `uv export … --prune polars --prune
scikit-learn` drops the two and everything only they pulled in (scipy, joblib,
threadpoolctl, narwhals, cloudpickle, polars' runtime), so "da does not have
scikit-learn" is a fact about the image and not a convention. One command, same
`uv.lock`, so no second resolution and no version skew between ds and da.

**`jupyter-server` is in both.** The da profile has the `notebook` slot too, and the
Jupyter plug *is* a jupyter-server (`docker/jupyter/launch.py`). A da box without it
could not serve its own notebook plug.

The bar is the same two-part bar, asked of the role: **would a data scientist / analyst
reach for it, and does it change what an agent can plausibly do?** Versions are what
`gateway/uv.lock` resolves for the extras in `pyproject.toml`, so the kernel and the
gateway's notebook tests mean the same thing by "pandas 3".

| package | ds | da | scripts | why it earns its place |
|---|---|---|---|---|
| `pandas==3.0.6` | ✓ | ✓ | — | the first import of every DS and DA notebook ever written. Without it the agent re-implements `groupby` in csvkit and shell, which is not how either role works and not a measurement of either |
| `pyarrow==25.0.1` | ✓ | ✓ | — | Parquet. The S3 landing zones and any lakehouse-shaped world speak it, and pandas 3 uses it as its string backend; a DS/DA who cannot `read_parquet` cannot open the data |
| `matplotlib==3.11.2` | ✓ | ✓ | — | a DA's deliverable is a chart as often as a number; "plot it before believing it" is the analyst's equivalent of the DE's `curl` |
| `ipykernel==7.4.0` | ✓ | ✓ | `ipython` | the kernel the Jupyter plug runs cells in, with the audit extension preloaded; `ipython` is how both roles poke at data interactively |
| `jupyter-server==2.21.1`, `jupyter-client==8.10.0` | ✓ | ✓ | `jupyter`, `jupyter-server`, `jupyter-kernelspec` | the Jupyter plug itself (`launch.py` execs `python -m jupyter_server`). Brings `terminado`; the plug runs with `terminals_enabled=False` |
| `nbclient==0.11.0`, `nbformat==5.11.1` | ✓ | ✓ | `jupyter-execute` | notebooks are the unit of DS/DA work; executing a stakeholder's `.ipynb` headlessly - through the audited kernel - is how an agent runs it rather than reads its JSON |
| `polars==1.44.2` | ✓ | ✗ | — | the dataframe a current DS reaches for when pandas is slow; an agent offered both choosing one is itself a signal. Not da: an analyst with polars has the DS box |
| `scikit-learn==1.9.1` | ✓ | ✗ | — | the DS role's distinguishing capability: fitting, cross-validating and scoring a model. Pulls `scipy` (112 MB installed) - the price of `sklearn`, not a separate choice. Not da, on purpose: roles must differ in what an agent *can do* |

Not added, deliberately: `jupyterlab` (a browser UI with no browser to load it),
`seaborn`/`plotly` (matplotlib answers the question; a second plotting stack changes
style, not capability), `statsmodels` (a real DS tool and a fair candidate, but not in
`pyproject.toml`'s `ds` extra; it goes there first, so the gateway and the box agree).
ds and da add **no CLI to `/opt/toolbelt`**: the jupyter scripts arrive with the
libraries in `/usr/local/bin`, so the venv lock is de's for every role and there are no
per-role venv locks.

**The kernelspec.** ds/da images install `alkera-python` system-wide at build time
(`install_kernelspec('/usr/local/share/jupyter', interpreter='/usr/local/bin/python')`)
and `cmp` it against the checked-in `docker/jupyter/kernels/alkera-python/kernel.json`,
so the build fails if the generator and the file drift. `launch.py` reinstalls the same
bytes at serve time. The launcher ships where all of `gateway/docker` does,
`/opt/stack/jupyter/`. The harness strips `/opt/stack` by `docker exec` into the
**episode** container only (`tasks/harness/container_episode.py`, `STRIP_PATHS`), so a
separate jupyter container from the same image keeps its launcher - and, by the same
token, keeps `/opt/stack` and `/opt/gateway/tests` readable from inside a notebook cell
unless its overlay strips them after start. That is for the compose/harness owner.

`toolbelt_check.py` is role-aware. For ds/da it runs `/usr/local/bin/python -c "import
pandas, pyarrow, matplotlib, ipykernel, jupyter_client, jupyter_server, nbclient,
nbformat, alkera_gateway.native.notebook[, polars, sklearn]"`, `jupyter kernelspec
list`, `jupyter server --version`, `jupyter-execute --version` and `ipython --version`,
and requires `alkera-python` in `jupyter kernelspec list --json`. For every role it
asserts that each package another role has is **absent**: not importable from
`/usr/local/bin/python` or `/opt/toolbelt/bin/python`, and no `jupyter*`/`ipython`
script in `/opt/toolbelt/bin` or `/usr/local/bin` - a `which` check would pass a PATH
mask; this does not.

### Warehouse clients

`--warehouse-clients snowflake` (build-arg `ALKERA_WAREHOUSE_CLIENTS`) installs
`gateway/requirements-snowflake.lock` - the `snowflake` extra: `snowflake-connector-python`
and `dbt-snowflake` - into the gateway's environment after `requirements.lock`. It is the
extra's whole closure from `uv export`, not a hand-trimmed diff: both are exported from
the one `uv.lock`, so the 84 packages they share carry identical versions (asserted by
the test) and pip reports them already satisfied; 14 install (`snowflake-connector-python`,
`dbt-snowflake`, `keyring`, `pyopenssl`, `asn1crypto`, `tomlkit`, …). Trimming would buy
nothing at install time and would cost the header's claim that the file is exactly what
that command emits.

---

## Determinism

Three pins, in the repo's existing idiom:

1. **Base image by digest.** `FROM python@sha256:9534e5a8…`, not `python:3.11-slim`. The
   tag moves; a rollout built from a different `3.11-slim` is a different measurement
   wearing the same name. (It resolved to Debian 13 *trixie*, not bookworm — worth knowing
   before pinning package versions by eye.)
2. **apt against `snapshot.debian.org`.** `ALKERA_DEBIAN_SNAPSHOT=20260910T000000Z`
   freezes the archive, so top-level *and* transitive package versions are fixed. Pinning
   only the top-level versions against live `deb.debian.org` would leave dependencies
   floating, and a pin against live Debian dies at the next point release with no way back.
   Cost, measured: ~25 s to fetch the snapshot indices, and the snapshot is a few days
   behind live security (`curl` is `8.14.1-2+deb13u4`; live has `u5`). In an image with no
   egress by construction that is an acceptable trade for a build that is the same bytes
   next month. Fallback if snapshot.debian.org is down: point the two `URIs:` lines at
   `http://deb.debian.org/debian` and `.../debian-security` and expect version drift.
3. **pip with `--require-hashes`**, for `requirements.lock` as before and now
   `toolbelt.lock` as well.

### arm64 (checked 2026-10-02, without Docker)

Every pin was resolved against `registry-1.docker.io/v2/<repo>/manifests/<digest>` with
the OCI-index and manifest-list `Accept` types. All are multi-arch indexes that list
`linux/arm64`, so **nothing was repinned**: `--platform linux/arm64` selects the arm64
child of the same digest.

| pin | where | media type | platforms |
|---|---|---|---|
| `python@sha256:9534e5a8…` | `Dockerfile`, `Dockerfile.subject` | OCI index | amd64, arm64/v8, arm/v5, arm/v7, 386, ppc64le, riscv64, s390x |
| `postgres@sha256:f1c3376c…` | `compose.yml` | OCI index | same eight |
| `adobe/s3mock@sha256:e7c36014…` | `compose.yml` | Docker manifest list | amd64, arm64 |
| `pgsty/minio@sha256:b6bfe723…` | `compose.native.yml` | OCI index | amd64, arm64 |
| `apache/airflow@sha256:d1a74224…` | `compose.native.yml` | OCI index | arm64, amd64 |

The rest of the arm64 path, checked the same way:

- **apt.** All 18 pinned versions are present in the `arm64` `Packages` indexes of
  `snapshot.debian.org/archive/{debian,debian-security}/20260910T000000Z` (trixie,
  trixie-updates, trixie-security).
- **toolbelt venv lock.** Compiled per arch (`--python-platform x86_64-…` →
  `toolbelt.lock`, `aarch64-…` → `toolbelt.arm64.lock`). `uv pip compile
  --generate-hashes` emits *every* file's hash for the chosen version, so the two differ
  only in their header line; they are kept as two files so a future marker-dependent
  resolution cannot silently diverge.
- **role and gateway locks** are `uv export`s of the universal `uv.lock`: one file
  serves both arches, with every wheel's hash and environment markers (numpy is pinned
  twice, `2.4.6 ; python_full_version < '3.12'` and `2.5.3` above; the image gets 2.4.6).
  `test_toolbelt_roles.py` evaluates the markers for CPython 3.11 on linux
  x86_64/aarch64, maps every lock's hashes to filenames through PyPI's JSON API, and
  requires an installable manylinux (or pure) wheel per arch for every package -
  numpy, pandas, pyarrow, polars' runtime, scikit-learn, scipy and matplotlib all carry
  `manylinux_2_17`/`_2_28` aarch64 wheels in the locks. No sdist builds: the image has
  no compiler.
- **That check found a live defect on amd64.** `toolbelt.lock` was compiled on
  2026-09-14 in the two hours between SQLAlchemy 2.0.53's sdist upload (20:37 UTC) and
  its wheels (22:39 onward), so it carried the sdist hash only and every build since has
  compiled SQLAlchemy from source, with unpinned build dependencies. Re-locked with
  `-P sqlalchemy==2.0.53`: same version, 52 wheel hashes added, no other line changed.
- **gateway locks** are `uv export`s of a universal `uv.lock`, so they carry all-arch
  hashes already. One package, `dbt-core-experimental-parser==2.0.0rc2` (via
  `dbt-core`), is sdist-only on PyPI: its build backend downloads a prebuilt wheel from
  the dbt-core GitHub release and verifies a sha256 embedded in the sdist. It lists
  `manylinux_2_28_aarch64`, so arm64 works - but the build needs github.com, which the
  rest of the build does not. Pre-existing on amd64.
- **DuckDB's `postgres` extension** for the pinned duckdb 1.5.5 is published for
  `linux_arm64` at extensions.duckdb.org (HTTP 200), so the baked `INSTALL postgres`
  has something to fetch.
- **Not verified:** an actual `docker buildx build --platform linux/arm64`. This host
  has no Docker. Until one is run and `toolbelt_check.py` is green inside it, arm64 is
  "every input resolves", not "builds".

Refreshing the apt pins: bump `ALKERA_DEBIAN_SNAPSHOT`, then

```sh
docker run --rm python@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534 \
  bash -c '... apt-cache policy <pkg> ...'
```

and copy the candidates in. A version that no longer resolves is a loud build failure,
which is the intent.

`pip install` also carries `--retries 5 --timeout 120`: measured on 2026-09-14, three of
five builds on this host lost a wheel to `files.pythonhosted.org … Read timed out` on
pip's 15 s default. That is a flaky build, not a flaky lock.

---

## Cost, measured

### Per role, before an image exists

No Docker on the host where the roles were added, so these are what *can* be measured
there: the wheels each role lock adds beyond the gateway's own (sizes from PyPI's JSON
for the exact file pip picks), and `site-packages` after `pip install --require-hashes`
of the locks in the Dockerfile's order into a CPython 3.11 venv on x86_64 (pip
byte-compiles, so `.pyc` is included, as it is in the image).

| role | packages added | wheels amd64 | wheels arm64 | system `site-packages` (x86_64) | largest installed |
|---|---|---|---|---|---|
| de | — | — | — | 314 MB | (the gateway, as today) |
| da | 66 | 117.3 MB | 114.7 MB | 815 MB (+501) | pyarrow 156, pandas 74, numpy 73, matplotlib 35, jedi 31 |
| ds | 74 | 213.6 MB | 205.1 MB | 1196 MB (+882) | polars runtime 172, pyarrow 156, scipy 142, pandas 74, sklearn 50 |

pyarrow is the single largest addition to either role and is not optional: it is
pandas 3's string backend. polars' runtime is the next; it is the one ds-only package
whose cost is worth re-litigating if image size starts to matter. The `/opt/toolbelt`
venv is unchanged for every role (240 MB, below).

### de, as built on 2026-09-14

| | before | after |
|---|---|---|
| `docker images` size | 961 MB | 1.36 GB |
| binaries the probe executes | — | 50, all green |

Where the 400 MB went, measured inside the image with `du -sm`:

| | MB |
|---|---|
| `/opt/toolbelt` total | 240 |
| — of which `awscli` (mostly `botocore` service models) | 151 |
| — of which `babel` (via `csvkit` → `agate`) | 33 |
| `/usr/share/vim` | 44 |
| everything else apt installed | ~115 |

`awscli` is 38 % of the growth for one binary. It stays: an object store the agent can
only reach through `s3_*` is an object store a data engineer does not recognise.

One naming wart worth knowing about: the credentials file ships as
`gateway/docker/aws-keys`, not `aws-credentials`, because the repository's `.gitignore`
line 6 is `*credentials*`. Named the obvious way it would be silently excluded from every
commit, and the clean-worktree build - which takes its context from git, not from the
working tree - would then fail on a `COPY` of a file that is not there. Found by running
`git check-ignore`, not by reasoning about it.

---

## Building and proving it

```sh
# build from a clean worktree at HEAD and measure the result from inside it
python gateway/docker/build_episode.py                  # -> evidence/image.json
python gateway/docker/build_episode.py --toolbelt ds    # -> alkera-episode:head-ds
python gateway/docker/build_episode.py --toolbelt da --warehouse-clients snowflake \
    --out gateway/docker/evidence/image-da-snowflake.json   # -> alkera-episode:head-da-snowflake
#   evidence records toolbelt {requested, label, probe, matches} and warehouse_clients;
#   a role mismatch between what was asked, the image label and the probe fails the run

# refresh the locks (each header carries its exact command); from gateway/:
uv pip compile docker/toolbelt.in --generate-hashes --python-version 3.11 \
  --python-platform x86_64-unknown-linux-gnu --output-file docker/toolbelt.lock
uv pip compile docker/toolbelt.in --generate-hashes --python-version 3.11 \
  --python-platform aarch64-unknown-linux-gnu --output-file docker/toolbelt.arm64.lock
uv export --locked --extra notebook --extra ds --no-dev --no-emit-project \
  --format requirements-txt --output-file requirements-ds.lock
uv export --locked --extra notebook --extra ds --prune polars --prune scikit-learn \
  --no-dev --no-emit-project --format requirements-txt --output-file requirements-da.lock
uv run pytest tests/test_toolbelt_roles.py -q   # roles, shared versions, a wheel per arch

# drive the tools against the live stack, from the agent's own shell
python gateway/docker/toolbelt_acceptance.py --project alkera-toolbelt   # -> evidence/toolbelt.json
#   23 live checks + 7 egress attempts made with the new tools, all of which must fail

# containment must be re-proven after any toolbelt change
python gateway/docker/containment.py --project alkera-check             # -> evidence/containment.json

# the parity tests
cd gateway && .venv/Scripts/python -m pytest tests/test_image_parity.py -v
```

`build_episode.py` stamps `org.alkera.source.revision` into the image, so what source a
container holds is a question with an answer instead of a memory. When the build carries
pending `gateway/docker/**` files that are not yet in that revision — the only overlay it
permits — `org.alkera.source.overlay` holds the sha256 of their contents and
`evidence/image.json` lists them, so "this is HEAD" and "this is HEAD plus a pending
Dockerfile change" can never be confused after the fact.

**Adding `curl`, `aws`, `nc`, `dig` and `ping` deliberately makes egress easy to attempt.**
That is the point — the agent *should* be able to try. The proof that it fails is
`containment.py`, which probes from inside each service's own network namespace and
carries a positive control. It must be re-run and re-recorded after every toolbelt change,
and nothing in this file may be used as a substitute for it.
