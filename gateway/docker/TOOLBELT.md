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
| `git` | `git` | the workspace is a real git repo; pre-existing. `/workspace` is a bind mount owned by the operator's uid and the agent is root, so the image marks exactly that path safe (`git config --system --add safe.directory /workspace`, as a dev container with a mounted checkout does); without it every `git status` died with `detected dubious ownership` (measured, 2026-10-04) |
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
| `csvkit==2.0.1` | `csvstat`, `csvlook`, `in2csv`, `csvsql`, `csvcut`, `csvgrep`, … | the landing zones are CSV (`revenue_daily`, `sftp_vendor`, `mainframe_batch`). `csvstat` profiles a partition without loading it into the warehouse first. It also pulls `openpyxl` and gives `in2csv` — the only shipped way to read `excel_close`'s `.xlsx` as tabular data. **2.0.1, not 2.2.0, since 2026-10-06**: csvkit ≥ 2.1 requires `agate>=1.12` and every `dbt-common` release pins `agate<1.10`, so one venv cannot hold both csvkit 2.2 and `mf` below. The commands listed are unchanged between the two; `agate` moved 1.14.2 → 1.9.1 and `pathspec` 1.1.1 → 1.0.4 (dbt's `<1.1`) with it |
| `dbt-metricflow[dbt-duckdb]==0.15.0` (→ `metricflow==0.213.0`), with `dbt-core==1.12.4`, `dbt-duckdb==1.11.0`, `duckdb==1.5.5` | `mf` | the world's dbt project carries a MetricFlow semantic layer (`dbt_project/models/semantic/`: `semantic_models:`, `metrics:`, saved queries), and the CLI a dbt shop answers "what is Net Revenue by month" with is `mf query --metrics net_revenue --group-by metric_time__month`; `mf validate-configs` and `mf list metrics` are how a metric change is checked before it is merged. Without it the semantic layer is YAML nobody can execute, and an agent asked whether a dashboard matches the governed metric has to hand-translate the metric into SQL — a different, error-prone task. In this venv and not the system interpreter because it pins its own dbt-core (`<1.13`); its dbt-core, dbt-duckdb and duckdb are pinned to the **gateway's** (`requirements.lock`, `test_toolbelt_roles.py` asserts it) so `mf`'s parse is the box's `dbt` parse and its DuckDB never writes the warehouse in a format the world's cannot read. Shimmed (`shims/install.py` DEFAULT_BINARIES): it writes `logs/` in the dbt project every call, `--csv` files where told, and opens the warehouse file; each call also runs `dbt debug` in-process, whose `git --help` leaves a `shim.git` row before mf's own. Needs `target/semantic_manifest.json` (`dbt parse` first) and the profile in `DBT_PROFILES_DIR`. dbt-metricflow, metricflow (Apache-2.0 since 0.209), dbt-core, dbt-duckdb Apache-2.0; duckdb MIT; the 41 other new transitive packages Apache-2.0/MIT/BSD |

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
| `openpyxl`/`pandas` in the *gateway* environment of a **de** image | Not done. `openpyxl` arrives inside `/opt/toolbelt` via `csvkit`, which gives `in2csv`. ds and da images *do* put pandas **and openpyxl** in the system interpreter ("Roles"), from the same `uv.lock` the gateway resolves; a de image does not, so a de box's tools behave exactly as before roles existed. openpyxl is therefore in two places on a ds/da box at one version (3.1.5): the venv's for `in2csv`, the system interpreter's for `pandas.read_excel`, which cannot see the venv's (measured 2026-10-06: `read_excel` failed in the da image's own Python until the `ds` extra carried it) |


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
| `openpyxl==3.1.5` | ✓ | ✓ | — | `.xlsx` in and out: `pandas.read_excel` / `to_excel` and openpyxl itself. Finance's spreadsheets are where a DA's numbers come from and go to; without it the analyst's own Python cannot open one (measured 2026-10-06), only `in2csv` from the venv can. The same version csvkit pins into `/opt/toolbelt`. Checked by `toolbelt_check.py`'s `SYSTEM_IMPORTS` and an openpyxl → `read_excel` round trip, against the system interpreter only - not `ROLE_IMPORTS`, whose absence check also reads the venv, where every role (de too) has csvkit's copy |
| `ipykernel==7.4.0` | ✓ | ✓ | `ipython` | the kernel the Jupyter plug runs cells in, with the audit extension preloaded; `ipython` is how both roles poke at data interactively |
| `jupyter-server==2.21.1`, `jupyter-client==8.10.0` | ✓ | ✓ | `jupyter`, `jupyter-server`, `jupyter-kernelspec` | the Jupyter plug itself (`launch.py` execs `python -m jupyter_server`). Brings `terminado`; the plug runs with `terminals_enabled=False` |
| `nbclient==0.11.0`, `nbformat==5.11.1` | ✓ | ✓ | `jupyter-execute` | notebooks are the unit of DS/DA work; executing a stakeholder's `.ipynb` headlessly - through the audited kernel - is how an agent runs it rather than reads its JSON. `python3`, the kernel nbclient and `jupyter-execute` start when not told otherwise, is the audited spec too; a kernel.json the agent writes into `~/.local/share/jupyter` is outside that guarantee (the in-box kernel is best-effort; the server plug is the guarantee) |
| `playwright==1.63.0` (+ `greenlet==3.5.6`, `pyee==13.0.1`) | ✓ | ✓ | `playwright` | the BI vendors' web UIs (Tableau Server's pages at `tableau.internal:8096`, Sigma's app at `sigma.internal:8097/alkera`) are where an analyst reads and edits a dashboard, and a real Playwright script over the shell is how an agent drives them (plan decision D5: a real client, server-side receipts - one `tableau.ui_*`/`sigma.ui_*` row per page, written before it is served - and no new agent tool surface). With the headless Chromium it pins (below). Pinned exactly in the `ds` extra because the version fixes the browser revision and the four sha256 the Dockerfile checks. Apache-2.0; greenlet MIT/PSF, pyee MIT. Probed by `toolbelt_check.py` (`import playwright`, `playwright --version`, `CHROMIUM_PROBE`) and toured by `tasks/harness/tool_tour_probes/browser.py` |
| `polars==1.44.2` | ✓ | ✗ | — | the dataframe a current DS reaches for when pandas is slow; an agent offered both choosing one is itself a signal. Not da: an analyst with polars has the DS box |
| `scikit-learn==1.9.1` | ✓ | ✗ | — | the DS role's distinguishing capability: fitting, cross-validating and scoring a model. Pulls `scipy` (112 MB installed) - the price of `sklearn`, not a separate choice. Not da, on purpose: roles must differ in what an agent *can do* |

Not added, deliberately: `jupyterlab` (rejected 2026-10-06 as "a browser UI with no
browser to load it"; **re-taken 2026-10-13**, now that ds/da have headless Chromium, and
still rejected, on bar 2: driving JupyterLab's JavaScript front-end through Playwright
runs cells in the same kernel `nbclient`, `jupyter-execute` and the plug's REST API
already reach, so it changes how an agent runs a cell, not what it can do - at the cost
of a second UI stack over the Jupyter plug (labextensions, a build of static assets) and
a JS-heavy page the agent must script blind; the BI UIs earn the browser because their
pages - sign-in, views, the formula editor - are a surface of their own that no REST
client exercises),
`seaborn`/`plotly` (matplotlib answers the question; a second plotting stack changes
style, not capability), `statsmodels` (a real DS tool and a fair candidate, but not in
`pyproject.toml`'s `ds` extra; it goes there first, so the gateway and the box agree).
ds and da add **no CLI to `/opt/toolbelt`**: the jupyter scripts arrive with the
libraries in `/usr/local/bin`, so the venv lock is de's for every role and there are no
per-role venv locks.

**The kernelspec.** ds/da images install the audited spec system-wide at build time,
as `alkera-python` and over ipykernel's own `python3`
(`install_audited_kernelspecs('/usr/local/share/jupyter', interpreter='/usr/local/bin/python')`),
and `cmp` each against the checked-in `docker/jupyter/kernels/alkera-python/kernel.json`,
so the build fails if the generator and the file drift. `launch.py` reinstalls the same
bytes at serve time. The launcher ships where all of `gateway/docker` does,
`/opt/stack/jupyter/`. The harness strips `/opt/stack` by `docker exec` into the
**episode** container only (`tasks/harness/container_episode.py`, `STRIP_PATHS`), so a
separate jupyter container from the same image keeps its launcher - and, by the same
token, keeps `/opt/stack` and `/opt/gateway/tests` readable from inside a notebook cell
unless its overlay strips them after start. That is for the compose/harness owner.

`toolbelt_check.py` is role-aware. For ds/da it runs `/usr/local/bin/python -c "import
pandas, pyarrow, matplotlib, ipykernel, jupyter_client, jupyter_server, nbclient,
nbformat, alkera_gateway.native.notebook, playwright[, polars, sklearn]"`, `jupyter
kernelspec list`, `jupyter server --version`, `jupyter-execute --version`, `ipython
--version`, `playwright --version` and `CHROMIUM_PROBE` (headless Chromium launched
through Playwright's default lookup, loading `about:blank`), and requires
`alkera-python` in `jupyter kernelspec list --json` and every spec it lists to preload
the audit extension (`kernelspecs.unaudited` empty). For every role it asserts that each
package another role has is **absent**: not importable from `/usr/local/bin/python` or
`/opt/toolbelt/bin/python`, and no `jupyter*`/`ipython`/`playwright` script in
`/opt/toolbelt/bin` or `/usr/local/bin` - a `which` check would pass a PATH mask; this
does not.

### Headless Chromium (ds, da)

Playwright drives a browser it does not ship in the wheel. The Dockerfile's chromium
step (ds/da only; harbor's `variant.BROWSER_LAYER` is the same step for a Harbor da
box, its pins and digests held equal by `test_bi_variant_installs_chromium_shell`):

| what | pinned by | notes |
|---|---|---|
| `chromium_headless_shell-1243` = Chrome for Testing **153.0.8010.12**, and `ffmpeg-1011` | Playwright 1.63.0's `browsers.json`, fetched by `playwright install --only-shell chromium` into `/opt/ms-playwright`; the two executables `sha256sum -c`'d per arch | downloaded at build time only (the zip from `cdn.playwright.dev` → `storage.googleapis.com/chrome-for-testing-public`); amd64 shell `ded93a9c…`, arm64 shell `f5d89353…`, ffmpeg `460d44f3…` / `b662355f…`. ffmpeg is what that command installs with the shell (Playwright's video recording); it is kept, so the box is what `playwright install` leaves on a laptop |
| the shared libraries the shell links | the frozen apt snapshot, top-level versions pinned like the toolbelt's (`libnss3`, `libgbm1`, `libglib2.0-0t64`, `libatk*`, `libx*`, `libasound2t64`, ...: its `readelf -d` NEEDED on both arches, which `libexpat1`/`libudev1` from the base already satisfy) | 47 packages land. `libgbm1` is most of the cost: it hard-depends on `mesa-libgallium` and `libllvm19`, ~200 MB for a library the shell only links (it renders with its bundled SwiftShader). No way around it from the snapshot short of a stub library, which was not done |
| `fontconfig-config`, `fonts-liberation` | the same snapshot | **load-bearing**: with no fontconfig config and no font the shell aborts on the first glyph it lays out (`SkFontMgr_FontConfigInterface.cpp: Not implemented`, measured 2026-10-13 - `about:blank` loads, any page with text kills the renderer). `fonts-liberation` is the font Chrome's own `.deb` depends on |

**Where Playwright finds it.** `/opt/ms-playwright` is fixed at install time
(`PLAYWRIGHT_BROWSERS_PATH` on the install command only) and linked from
`~/.cache/ms-playwright` - Playwright's default lookup - for root, the episode agent
(Harbor: for `engineer` and root). The box's environment gains **no**
`PLAYWRIGHT_BROWSERS_PATH`: this one-stage Dockerfile cannot set an `ENV` for ds/da
alone, and an `ENV` for every role would change a de box and so every unprofiled
episode. `p.chromium.launch()` - what an agent writes - just works; `headless=False`
fails (no display), which is the truth about the box.

**Rejected: Debian's `chromium-headless-shell`.** The snapshot has
`152.0.7977.82-1~deb13u1`, one major behind the Chromium 153 Playwright 1.63 is built
against and guarantees; it needs `chromium-common` (65 MB) on top of the same
`libgbm1` chain; and Playwright would only use it through `executable_path`, so the
`launch()` an agent writes would fail without a symlink impersonating Playwright's own
download. Playwright's pinned shell is one download, verified by digest, and is what
`playwright install` gives every laptop.

**At run time it needs no network.** The browser runs `--remote-debugging-pipe`
(no TCP listener), `--headless --no-sandbox` and Playwright's default flags
(`--disable-background-networking`, `--disable-component-update`, `--disable-sync`,
...), read off its own `/proc/<pid>/cmdline`. Measured 2026-10-13 on da/laptop
(BI001's box, `alkera-episode:t17-da`), sampling the episode's `/proc/net/{tcp,udp}{,6}`
every 20 ms while a script signed in to both UIs and opened a page in each: no
listener added (the only ones are Docker's embedded DNS, before and after), the only
peers native-services' 8096 and 8097, `destinations.jsonl` +1 `tableau.internal:8096`
and +1 `sigma.internal:8097` and nothing else, and containment P7
(`containment.py --native-episode`) passed, all eleven of its probe checks true.
Playwright re-validates the browser's shared libraries (`ldd`) when its
`DEPENDENCIES_VALIDATED` marker is older than 30 days and tries to touch it; in
Harbor the marker's directory is root's and the touch fails silently - a few hundred
milliseconds per launch after a month, nothing more.

**Refreshing.** Bump `playwright` in `pyproject.toml`'s `ds` extra, `uv lock`, re-export
both role locks, then read the new `driver/package/browsers.json` for the
`chromium-headless-shell` and `ffmpeg` revisions, fetch their zips for both arches and
replace the revision directories and the four sha256 in the Dockerfile and
`variant.BROWSER_LAYER`. A digest that no longer matches is a loud build failure, which
is the intent.

### Warehouse clients

`--warehouse-clients <c>[,<c>...]` (build-arg `ALKERA_WAREHOUSE_CLIENTS`) installs
`gateway/requirements-<c>.lock` for each set into the gateway's environment after
`requirements.lock`. The sets are the registry's (`build_episode.WAREHOUSE_CLIENTS`, from
`alkera_gateway.backends`): `airflow`, `bigquery`, `databricks`, `redshift`, `snowflake`,
`spark`. Each lock is compiled apart from `gateway/uv.lock` by
`lock_warehouse_clients.py <c>` (snowflake's header names the `lock_snowflake_clients.py`
wrapper) from `docker/<c>-clients.in`, constrained to every version the base, native and
ds locks pin. A package a set shares with them is therefore the same version and pip
reports it already satisfied; the one deliberate exception is certifi, which
dbt-snowflake caps below 2025.4.26 (`<c>-clients.overrides`). Compiling them inside
`uv.lock` would hold back every image's CA bundle.

**What a client set may bring onto a role that forbids it.** The role gate forbids every
module and console script another role has (`forbidden_imports`, `system_forbidden`,
`forbidden_binaries`). Two sets require some of those as hard dependencies, so
`toolbelt_check.py` allows exactly those, for each set the image installed
(`installed_clients()`: the `/opt/gateway/requirements-<c>.lock` files it kept). The
report lists them as `client_allowed_imports`.

| set | `CLIENT_IMPORTS` | `CLIENT_BINARIES` | why |
|---|---|---|---|
| `databricks` | `openpyxl`, `pandas`, `pyarrow` | — | databricks-sql-connector requires pandas and openpyxl; dbt-databricks requires the connector with its pyarrow extra |
| `bigquery` | `nbformat`, `pandas`, `pyarrow` | `jupyter` | dbt-bigquery requires `google-cloud-bigquery[pandas]` and nbformat (plan decision D2, `docker/bigquery-clients.in`); nbformat requires jupyter_core, whose `jupyter` dispatcher is all it brings |

The rest stays forbidden on a de box with either set: no kernel, no Jupyter server, no
nbclient, matplotlib, polars or scikit-learn, and `jupyter server` / `kernelspec` /
`execute` name scripts the image does not have. The bigquery set's pandas, pyarrow and
nbformat are the versions the da/ds role locks pin, so one interpreter has one pandas
(`test_bigquery_clients_may_carry_pandas_and_nbformat_in_de`,
`test_jupyter_script_from_a_client_lock_is_not_a_role_leak`). Cost: adding
dbt-bigquery to the set made the de image 649 MB larger (`alkera-episode:t13-bigquery`,
2554.7 MB, over the set without it; mostly pyarrow, pandas, numpy and
google-cloud-aiplatform).

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
- **Playwright and its browser** (checked 2026-10-13, without an arm64 build).
  `playwright==1.63.0` has a `manylinux_2_17_aarch64` wheel hashed in both role locks
  (greenlet one too; pyee is pure), which `test_every_package_has_a_wheel_for_its_arch`
  checks. Playwright resolves trixie on arm64 to `debian13-arm64`, whose
  chromium-headless-shell download is Chrome for Testing's
  `153.0.8010.12/linux-arm64/chrome-headless-shell-linux-arm64.zip` (HTTP 200,
  120,278,638 bytes) and whose ffmpeg is `ffmpeg/1011/ffmpeg-linux-arm64.zip`; both were
  fetched and their executables hashed into the Dockerfile's `arm64)` branch. The arm64
  shell's NEEDED list is the amd64 one minus `libatk-bridge-2.0`; every apt pin of the
  chromium step is in the snapshot's arm64 indexes at the same version. Still "every
  input resolves", not "builds".
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

### da + Playwright and headless Chromium, as built on 2026-10-13

`alkera-episode:t17-da` against `alkera-episode:t11-da` (the same role from the tree
it was cut from, without Playwright), x86_64:

| | before | after | delta |
|---|---|---|---|
| `docker image inspect` size | 2,548,489,913 B | 3,448,738,334 B | +900 MB |
| sum of `docker history` layers (uncompressed content) | 1,982.6 MB | 2,640.9 MB | **+658 MB** |
| — the role's pip layer | 538 MB | 687 MB | +149 MB: `playwright` 140 MB (its bundled node driver is most of it), greenlet 3, pyee 1 |
| — the chromium step (new) | — | 509 MB | the shell 261 MB, ffmpeg 5 MB, 47 apt packages ~243 MB (`libLLVM.so.19.1` 124 MB and `libgallium` 41 MB of it, via `libgbm1`; fonts 5 MB) |

47 packages added, none upgraded or removed (`dpkg-query` of both images). de is
untouched: its chromium step only prints, and `toolbelt_check.py` forbids `playwright`
there. The UI_CLONE_PLAN estimate was ~300 MB; the difference is
the node driver Playwright for Python ships and the mesa/LLVM chain `libgbm1` drags in.
ds carries the same step and was not measured; Harbor's ds/da variants carry it too
(`variant.BROWSER_ROLES`).

**Follow-up (plan decision D5's cost, not done):** move the browser layer to an opt-in
variant, so that only a box whose task drives the BI UIs pays the +900 MB. Today every
da and ds image carries it, BI001/BI002's included, whether or not a task opens a page.

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
