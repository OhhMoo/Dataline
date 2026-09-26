#!/usr/bin/env bash
# Set up a Linux host to run Alkera-DE rollouts (native driver, container topology).
#
#   scripts/setup_linux.sh                    # auto: podman if present, else Docker CE
#   scripts/setup_linux.sh --engine docker    # Docker CE (rootful daemon), what most labs run
#   scripts/setup_linux.sh --engine podman    # rootless podman behind the docker CLI
#   scripts/setup_linux.sh --skip-packages    # already have an engine; venv, images, checks only
#
# Supported: Fedora / RHEL-family (dnf) and Ubuntu / Debian (apt). Uses sudo for
# packages only; everything else is per-user. Idempotent: re-run it after a pull.
#
# What it does, in order:
#   1. OS packages: git, curl, jq, bash, a container engine and the compose v2 plugin
#   2. uv, and gateway/.venv from gateway/uv.lock (Python 3.11, matching the image)
#   3. the claude CLI, if missing
#   4. pulls every pinned third-party image the stack uses, builds alkera-episode:head
#      and alkera-subject:head (the hard sandbox, tasks/harness/SANDBOX.md)
#   5. tasks/harness/linux_preflight.py
#
# It does NOT create a subject credential. After it finishes:
#   claude setup-token                          # prints a long-lived token
#   export CLAUDE_CODE_OAUTH_TOKEN=<token>      # or ANTHROPIC_API_KEY=...
# Why: tasks/harness/LINUX.md, "The subject's login".
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE=auto
SKIP_PACKAGES=0
SKIP_IMAGE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --engine) ENGINE="$2"; shift 2 ;;
    --skip-packages) SKIP_PACKAGES=1; shift ;;
    --skip-image) SKIP_IMAGE=1; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Linux ] || die "this script is for Linux"
[ "$(id -u)" -ne 0 ] || die "run as your own user, not root (sudo is used where needed)"
. /etc/os-release
case " ${ID} ${ID_LIKE:-} " in
  *" fedora "*|*" rhel "*|*" centos "*) PM=dnf ;;
  *" ubuntu "*|*" debian "*) PM=apt ;;
  *) die "unsupported distribution ${ID}; install docker + compose v2 by hand and pass --skip-packages" ;;
esac

if [ "$ENGINE" = auto ]; then
  if command -v docker >/dev/null && docker info >/dev/null 2>&1; then ENGINE=existing
  elif command -v podman >/dev/null; then ENGINE=podman
  else ENGINE=docker; fi
fi
say "engine: $ENGINE, package manager: $PM"

# ---------------------------------------------------------------- 1. packages
install_docker_dnf() {
  sudo dnf -y install dnf-plugins-core
  if dnf --version 2>/dev/null | grep -q '^dnf5'; then
    sudo dnf config-manager addrepo --overwrite \
      --from-repofile="https://download.docker.com/linux/${ID/rhel/centos}/docker-ce.repo"
  else
    sudo dnf config-manager --add-repo \
      "https://download.docker.com/linux/${ID/rhel/centos}/docker-ce.repo"
  fi
  # podman-docker also owns /usr/bin/docker
  sudo dnf -y remove podman-docker 2>/dev/null || true
  sudo dnf -y install docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
}

install_docker_apt() {
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
}

if [ "$SKIP_PACKAGES" = 0 ]; then
  say "1. OS packages"
  if [ "$PM" = dnf ]; then
    sudo dnf -y install git curl jq bash tar gzip
  else
    sudo apt-get update && sudo apt-get install -y git curl jq bash ca-certificates tar gzip
  fi
  case "$ENGINE" in
    docker)
      if [ "$PM" = dnf ]; then install_docker_dnf; else install_docker_apt; fi
      sudo systemctl enable --now docker
      if ! id -nG "$USER" | tr ' ' '\n' | grep -qx docker; then
        sudo usermod -aG docker "$USER"
        echo "   added $USER to the docker group: log out and back in (or run 'newgrp docker'),"
        echo "   then re-run: scripts/setup_linux.sh --skip-packages"
        exit 0
      fi
      ;;
    podman)
      # podman-docker puts a `docker` on PATH that is podman; `docker compose`
      # then runs the compose v2 plugin against the rootless podman socket.
      if [ "$PM" = dnf ]; then
        sudo dnf -y install podman podman-docker docker-compose aardvark-dns netavark
      else
        sudo apt-get install -y podman podman-docker docker-compose-v2 || \
          sudo apt-get install -y podman podman-docker docker-compose-plugin
      fi
      sudo touch /etc/containers/nodocker          # silence the emulation banner
      systemctl --user enable --now podman.socket
      # The compose files pin images by short name (postgres@sha256:...). Rootless
      # podman's default short-name mode prompts on a TTY and fails without one.
      mkdir -p "$HOME/.config/containers"
      conf="$HOME/.config/containers/registries.conf"
      if ! grep -qs 'alkera' "$conf"; then
        cat >>"$conf" <<'EOF'
# alkera: resolve the stack's short image names on docker.io, without a prompt
unqualified-search-registries = ["docker.io"]
short-name-mode = "permissive"
EOF
      fi
      sock="unix://${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
      rc="$HOME/.bashrc"
      grep -qs 'alkera: podman socket' "$rc" || \
        printf '\n# alkera: podman socket for docker compose\nexport DOCKER_HOST=%s\n' "$sock" >>"$rc"
      export DOCKER_HOST="$sock"
      ;;
    existing) echo "   using the docker already on PATH: $(docker --version)" ;;
    *) die "--engine must be docker or podman" ;;
  esac
fi

command -v docker >/dev/null || die "no docker CLI after step 1"
docker info >/dev/null 2>&1 || die "docker CLI cannot reach an engine (group membership? DOCKER_HOST?)"
docker compose version >/dev/null 2>&1 || die "no docker compose v2 plugin"
echo "   $(docker compose version)"

# ---------------------------------------------------------------- 2. python
say "2. uv and gateway/.venv"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
( cd "$REPO/gateway" && uv sync --locked --python 3.11 --extra native --extra dbt )
"$REPO/gateway/.venv/bin/python" -c "import alkera_gateway, duckdb, dbt; print('   venv ok')"

# ---------------------------------------------------------------- 3. claude CLI
say "3. claude CLI"
if ! command -v claude >/dev/null; then
  curl -fsSL https://claude.ai/install.sh | bash
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "   $(claude --version)"

# ---------------------------------------------------------------- 4. images
say "4. images"
# Every third-party image the stack and the two Dockerfiles use, pulled now so the
# first rollout does not spend its wall clock on a download. Read from the files
# rather than listed here, so a re-pin cannot drift from this script. Only
# digest-pinned references are pulled: everything named alkera-* is built below,
# and asking a registry for it fails (`docker compose pull` does exactly that).
# Fully qualified with docker.io, because rootless podman will not guess a
# registry for a short name without a TTY.
images="$(grep -hoE '^[[:space:]]*(image:|FROM)[[:space:]]+[a-z0-9./_-]+@sha256:[0-9a-f]{64}' \
    "$REPO"/gateway/docker/compose*.yml "$REPO"/gateway/docker/Dockerfile* \
  | awk '{print $2}' | sort -u)"
for ref in $images; do
  case "$ref" in
    */*/*) full="$ref" ;;                      # already has a registry
    */*)   full="docker.io/$ref" ;;            # org/name
    *)     full="docker.io/library/$ref" ;;    # official image
  esac
  echo "   pull ${full%%@*}"
  docker pull -q "$full" >/dev/null
done
if [ "$SKIP_IMAGE" = 0 ]; then
  if [ -n "$(git -C "$REPO" status --porcelain -- gateway world)" ]; then
    echo "   NOTE: gateway/ or world/ has uncommitted changes. The image is built from"
    echo "   HEAD, and the harness refuses an image older than HEAD's gateway/world:"
    echo "   commit, then re-run with --skip-packages."
  fi
  "$REPO/gateway/.venv/bin/python" "$REPO/gateway/docker/build_episode.py"
  # The hard-sandbox subject (tasks/harness/SANDBOX.md): the model's CLI in its
  # own container, pinned to the CLI version on this host.
  claude_version="$(claude --version | awk '{print $1}')"
  docker build -f "$REPO/gateway/docker/Dockerfile.subject" \
    --build-arg CLAUDE_VERSION="$claude_version" \
    -t alkera-subject:head "$REPO/gateway/docker/subject"
fi

# ---------------------------------------------------------------- 5. preflight
say "5. preflight"
"$REPO/gateway/.venv/bin/python" "$REPO/tasks/harness/linux_preflight.py" || true
cat <<EOF

Next:
  claude setup-token && export CLAUDE_CODE_OAUTH_TOKEN=<token>
  gateway/.venv/bin/python tasks/harness/linux_preflight.py --live --model-smoke opus
  gateway/.venv/bin/python tasks/harness/run_rollouts.py --task GT001-aov-halving \\
      --variant decision --model scripted --k 1   # full rollout through the sandbox, no quota
  scripts/run_decision_pass1.sh --k 1          # then the full run
EOF
