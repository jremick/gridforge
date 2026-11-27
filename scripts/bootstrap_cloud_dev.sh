#!/usr/bin/env bash
# Idempotent bootstrap script for a Linux cloud development VM (Debian 12/13 or Ubuntu 22.04/24.04)
# running native Docker Engine, Node.js 22 LTS, GitHub CLI, Tailscale, and optional idle auto-shutdown.
#
# Usage:
#   sudo ./scripts/bootstrap_cloud_dev.sh [options]
#
# Options:
#   --user <name>                    Linux user to add to the docker group (default: SUDO_USER or current user)
#   --idle-shutdown-minutes <N>      Install systemd timer to power off after N idle minutes (0 = disabled, default: 0)
#   --dry-run                        Print planned actions without modifying the system
#   -h, --help                       Show this help message
set -euo pipefail

DEV_USER="${SUDO_USER:-$(id -un)}"
IDLE_SHUTDOWN_MINUTES="0"
DRY_RUN="false"

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)
      if [[ $# -lt 2 ]]; then
        echo "--user requires a value." >&2
        usage >&2
        exit 1
      fi
      DEV_USER="$2"
      shift 2
      ;;
    --idle-shutdown-minutes)
      if [[ $# -lt 2 ]]; then
        echo "--idle-shutdown-minutes requires a value." >&2
        usage >&2
        exit 1
      fi
      IDLE_SHUTDOWN_MINUTES="$2"
      shift 2
      ;;
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if ! [[ "${IDLE_SHUTDOWN_MINUTES}" =~ ^[0-9]+$ ]]; then
  echo "--idle-shutdown-minutes must be a non-negative integer (got '${IDLE_SHUTDOWN_MINUTES}')." >&2
  exit 1
fi

log() {
  printf '[cloud-dev] %s\n' "$*"
}

if [[ "${DRY_RUN}" == "true" ]]; then
  log "DRY RUN enabled (no changes will be made)."
  log "Target user: ${DEV_USER}"
  log "Idle auto-shutdown minutes: ${IDLE_SHUTDOWN_MINUTES}"
  log "Planned components: base CLI tools, Docker Engine + Compose v2 + Buildx, Node.js 22 LTS, GitHub CLI (gh), Tailscale"
  if [[ "${IDLE_SHUTDOWN_MINUTES}" -gt 0 ]]; then
    log "Planned systemd timer: gridforge-idle-shutdown.timer (threshold: ${IDLE_SHUTDOWN_MINUTES}m)"
  fi
  exit 0
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run this script with sudo or as root." >&2
  exit 1
fi

if [[ ! -r /etc/os-release ]]; then
  echo "Unsupported OS: /etc/os-release not found (requires Debian or Ubuntu)." >&2
  exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
DISTRO_ID="${ID:-}"
CODENAME="${VERSION_CODENAME:-}"
if [[ "${DISTRO_ID}" != "ubuntu" && "${DISTRO_ID}" != "debian" ]]; then
  echo "Unsupported distribution '${DISTRO_ID}'. This script supports Debian and Ubuntu." >&2
  exit 1
fi
if [[ -z "${CODENAME}" ]]; then
  echo "Could not determine VERSION_CODENAME from /etc/os-release." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

log "Installing base development tools on ${DISTRO_ID} (${CODENAME})..."
apt-get update -y
apt-get install -y --no-install-recommends \
  apt-transport-https \
  build-essential \
  ca-certificates \
  curl \
  git \
  gnupg \
  jq \
  lsb-release \
  poppler-utils \
  python3 \
  python3-pip \
  python3-venv \
  ripgrep \
  tmux

install -m 0755 -d /etc/apt/keyrings

# 1. Native Docker Engine + Compose v2 + Buildx
if ! command -v docker >/dev/null 2>&1; then
  log "Installing native Docker Engine + Buildx + Compose plugin..."
  curl -fsSL "https://download.docker.com/linux/${DISTRO_ID}/gpg" | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  ARCH="$(dpkg --print-architecture)"
  cat >/etc/apt/sources.list.d/docker.list <<EOF
deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/${DISTRO_ID} ${CODENAME} stable
EOF
  apt-get update -y
  apt-get install -y --no-install-recommends \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin
else
  log "Docker already installed: $(docker --version)"
fi

log "Configuring Docker daemon log rotation and BuildKit defaults..."
mkdir -p /etc/docker
if [[ ! -f /etc/docker/daemon.json ]]; then
  cat >/etc/docker/daemon.json <<'EOF'
{
  "features": {
    "buildkit": true
  },
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
EOF
fi
systemctl enable --now docker

if id "${DEV_USER}" >/dev/null 2>&1; then
  usermod -aG docker "${DEV_USER}"
  log "Added '${DEV_USER}' to the docker group."
else
  log "Warning: user '${DEV_USER}' does not exist yet; skipping docker group membership."
fi

# 2. Node.js 22 LTS
NODE_MAJOR="$(node -v 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/' || true)"
if [[ "${NODE_MAJOR}" != "22" ]]; then
  log "Installing Node.js 22 LTS..."
  curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg
  chmod a+r /etc/apt/keyrings/nodesource.gpg
  cat >/etc/apt/sources.list.d/nodesource.list <<'EOF'
deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main
EOF
  apt-get update -y
  apt-get install -y --no-install-recommends nodejs
else
  log "Node.js 22 already installed: $(node -v)"
fi

# 3. GitHub CLI (gh)
if ! command -v gh >/dev/null 2>&1; then
  log "Installing GitHub CLI (gh)..."
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
  chmod a+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  ARCH="$(dpkg --print-architecture)"
  cat >/etc/apt/sources.list.d/github-cli.list <<EOF
deb [arch=${ARCH} signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main
EOF
  apt-get update -y
  apt-get install -y --no-install-recommends gh
else
  log "GitHub CLI already installed: $(gh --version | head -n1)"
fi

# 4. Tailscale (private Mesh VPN + Tailscale SSH + Tailscale Serve)
if ! command -v tailscale >/dev/null 2>&1; then
  log "Installing Tailscale..."
  curl -fsSL "https://pkgs.tailscale.com/stable/${DISTRO_ID}/${CODENAME}.noarmor.gpg" -o /etc/apt/keyrings/tailscale-archive-keyring.gpg
  chmod a+r /etc/apt/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL "https://pkgs.tailscale.com/stable/${DISTRO_ID}/${CODENAME}.tailscale-keyring.list" \
    | sed 's|/usr/share/keyrings/tailscale-archive-keyring.gpg|/etc/apt/keyrings/tailscale-archive-keyring.gpg|g' \
    >/etc/apt/sources.list.d/tailscale.list
  apt-get update -y
  apt-get install -y --no-install-recommends tailscale
  systemctl enable --now tailscaled
else
  log "Tailscale already installed: $(tailscale version | head -n1)"
fi

# 5. Optional idle auto-shutdown timer (useful for pay-per-minute GCP/AWS dev VMs)
if [[ "${IDLE_SHUTDOWN_MINUTES}" -gt 0 ]]; then
  log "Installing idle auto-shutdown timer (${IDLE_SHUTDOWN_MINUTES} minutes)..."
  cat >/usr/local/bin/gridforge-idle-shutdown.sh <<EOF
#!/usr/bin/env bash
set -euo pipefail
THRESHOLD_MINUTES=${IDLE_SHUTDOWN_MINUTES}
STATE_FILE=/run/gridforge-idle-since

# Consider the VM active if any interactive login/SSH/Tailscale SSH session,
# tmux server, running Docker container/build, pytest run, or remote IDE/Vite
# server process is present.
is_busy() {
  if [[ -n "\$(who 2>/dev/null || true)" ]]; then
    return 0
  fi
  if ps -eo tty= 2>/dev/null | grep -qE '^[[:space:]]*pts/'; then
    return 0
  fi
  if pgrep -x tmux >/dev/null 2>&1; then
    return 0
  fi
  if command -v docker >/dev/null 2>&1 && [[ -n "\$(docker ps -q 2>/dev/null || true)" ]]; then
    return 0
  fi
  if pgrep -f "antigravity|vscode-server|node.*vite|docker.*build|pytest" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

if is_busy; then
  rm -f "\${STATE_FILE}"
  exit 0
fi

NOW=\$(date +%s)
if [[ ! -f "\${STATE_FILE}" ]]; then
  echo "\${NOW}" > "\${STATE_FILE}"
  exit 0
fi

IDLE_SINCE=\$(cat "\${STATE_FILE}")
ELAPSED=\$(( (NOW - IDLE_SINCE) / 60 ))
if [[ "\${ELAPSED}" -ge "\${THRESHOLD_MINUTES}" ]]; then
  logger -t gridforge-idle-shutdown "Host idle for \${ELAPSED}m (>= \${THRESHOLD_MINUTES}m); powering off."
  rm -f "\${STATE_FILE}"
  /sbin/shutdown -h now "Auto-shutdown after \${ELAPSED}m idle"
fi
EOF
  chmod 0755 /usr/local/bin/gridforge-idle-shutdown.sh

  cat >/etc/systemd/system/gridforge-idle-shutdown.service <<'EOF'
[Unit]
Description=GridForge cloud dev VM idle check

[Service]
Type=oneshot
ExecStart=/usr/local/bin/gridforge-idle-shutdown.sh
EOF

  cat >/etc/systemd/system/gridforge-idle-shutdown.timer <<'EOF'
[Unit]
Description=Check every 5 minutes whether the GridForge cloud dev VM is idle

[Timer]
OnBootSec=10min
OnUnitActiveSec=5min
Unit=gridforge-idle-shutdown.service

[Install]
WantedBy=timers.target
EOF

  systemctl daemon-reload
  systemctl enable --now gridforge-idle-shutdown.timer
fi

log "Bootstrap complete!"
cat <<EOF

Next steps on this VM:
  1. Connect to your private Tailnet with Tailscale SSH enabled:
       sudo tailscale up --ssh
  2. Re-login (or run 'newgrp docker') so '${DEV_USER}' picks up docker group membership.
  3. Authenticate GitHub CLI and clone GridForge:
       gh auth login
       git clone https://github.com/jremick/gridforge.git
  4. Connect from your laptop using Antigravity SSH Host (see deploy/cloud-dev.md).
EOF
