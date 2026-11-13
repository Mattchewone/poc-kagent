#!/usr/bin/env bash
# Host prerequisites for `make up`. kind and kagent are pinned in .cache/bin by scripts/tools.sh;
# everything else must be on PATH. Exits non-zero on anything missing.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
bad=0
ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
miss() { printf '  \033[31mmiss\033[0m %s\n' "$*"; bad=1; }
warn() { printf '  \033[33mwarn\033[0m %s\n' "$*"; }
need() { # cmd hint
  if command -v "$1" >/dev/null; then ok "$1"; else miss "$1  ($2)"; fi
}

echo "tools"
need docker    "Docker Desktop, OrbStack or colima"
need kubectl   "brew install kubectl"
need helm      "brew install helm"
need uv        "brew install uv"
need temporal  "brew install temporal"
need openshell "OpenShell CLI v0.1.8: https://github.com/NVIDIA/OpenShell/releases"
need node      "Node 20+ for demo-app tests (brew install node)"
need git       "xcode-select --install"
need curl      "-"
need openssl   "-"
command -v k9s >/dev/null && ok "k9s (optional)" || warn "k9s not found (optional: brew install k9s)"

echo "docker"
if docker info >/dev/null 2>&1; then
  ok "daemon running"
  mem=$(docker info --format '{{.MemTotal}}' 2>/dev/null || echo 0)
  gb=$(( mem / 1024 / 1024 / 1024 ))
  if [ "$gb" -ge 12 ]; then ok "${gb} GiB memory"
  else warn "${gb} GiB memory for Docker: four kind clusters want 12+ GiB (Docker Desktop > Settings > Resources)"; fi
else
  miss "docker daemon not running"
fi

echo "config"
if [ -f .env ] && grep -q '^ANTHROPIC_API_KEY=.' .env; then ok ".env has ANTHROPIC_API_KEY"
else warn ".env has no ANTHROPIC_API_KEY: agents won't answer until you set it (cp .env.example .env; make kagent-key)"; fi
[ -x .venv/bin/python ] && ok ".venv" || warn ".venv missing: make setup (make up runs it for you)"

[ $bad = 0 ] && echo "all required tools present" || { echo "install the missing tools above, then re-run"; exit 1; }
