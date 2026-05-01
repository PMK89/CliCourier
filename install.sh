#!/bin/sh
set -eu

# ---- Repository configuration -------------------------------------------------
# Maintainers: replace this placeholder before publishing install.sh from GitHub.
# Users can override it:
#   CLICOURIER_REPO_URL=https://github.com/PMK89/CliCourier.git sh install.sh
DEFAULT_REPO_URL="https://github.com/PMK89/CliCourier.git"
REPO_URL="${CLICOURIER_REPO_URL:-$DEFAULT_REPO_URL}"
# -----------------------------------------------------------------------------

info() {
  printf '%s\n' "$*"
}

fail() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

case "$(uname -s 2>/dev/null || true)" in
  Linux) ;;
  *) fail "CliCourier currently supports Linux and Windows via WSL only." ;;
esac

if grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null; then
  PLATFORM="WSL"
else
  PLATFORM="Linux"
fi

command -v python3 >/dev/null 2>&1 || fail "python3 is required."
command -v git >/dev/null 2>&1 || fail "git is required."
command -v curl >/dev/null 2>&1 || fail "curl is required."

if ! python3 - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
then
  fail "Python 3.11 or newer is required."
fi

if ! command -v uv >/dev/null 2>&1; then
  info "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
fi

command -v uv >/dev/null 2>&1 || fail "uv was installed but is not available on PATH."

is_clicourier_checkout() {
  [ -d ".git" ] && [ -f "pyproject.toml" ] || return 1
  python3 - <<'PY'
from pathlib import Path
import tomllib

try:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8")).get("project", {})
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if project.get("name") == "cli-courier" else 1)
PY
}

if is_clicourier_checkout; then
  INSTALL_TARGET="$(pwd)"
else
  INSTALL_TARGET="git+$REPO_URL"
fi

info "Platform: $PLATFORM"
if command -v clicourier >/dev/null 2>&1 || command -v cli-courier >/dev/null 2>&1; then
  info "Existing CliCourier install detected; updating from: $INSTALL_TARGET"
else
  info "Installing CliCourier as a uv tool from: $INSTALL_TARGET"
fi
uv tool install --force --upgrade --reinstall-package cli-courier "$INSTALL_TARGET"

mkdir -p "$HOME/.config/clicourier"
mkdir -p "$HOME/.local/state/clicourier"

if ! command -v clicourier >/dev/null 2>&1; then
  if [ -x "$HOME/.local/bin/clicourier" ]; then
    info "clicourier installed at $HOME/.local/bin/clicourier"
    info "Add this to your shell PATH if needed:"
    info "  export PATH=\"\$HOME/.local/bin:\$PATH\""
  else
    fail "uv completed but clicourier was not found. Check uv tool dir with: uv tool dir"
  fi
fi

info ""
info "CliCourier installed."
info "Next steps:"
info "  clicourier init"
info "  clicourier doctor"
info "  clicourier model download"
info "  clicourier run"
