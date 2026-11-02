#!/bin/sh
# tabnap installer:  curl -fsSL https://raw.githubusercontent.com/rhuthik/tabnap/main/install.sh | sh
#
# Installs the `tabnap` command with uv (or pipx), then runs `tabnap install`, which asks which
# features you want and sets up iTerm2 / tmux. Nothing is installed system-wide.
set -eu

SOURCE="${TABNAP_SOURCE:-git+https://github.com/rhuthik/tabnap}"
if [ "$(uname)" = "Darwin" ]; then
  SPEC="tabnap[iterm] @ $SOURCE"   # iTerm2 support needs the iterm2 Python package
else
  SPEC="tabnap @ $SOURCE"
fi

say() { printf '%s\n' "$*"; }
ask() {  # ask "question" -> 0 for yes
  printf '%s [Y/n] ' "$1"
  read -r answer </dev/tty || answer=""
  case "$answer" in [nN]*) return 1 ;; *) return 0 ;; esac
}

if command -v uv >/dev/null 2>&1; then
  say "Installing tabnap with uv..."
  uv tool install --force --python ">=3.11" "$SPEC"
elif command -v pipx >/dev/null 2>&1; then
  say "Installing tabnap with pipx..."
  pipx install --force "$SPEC"
else
  say "tabnap installs with uv (https://docs.astral.sh/uv) or pipx, and neither is installed."
  if ask "Install uv now (from astral.sh)?"; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    UV="$HOME/.local/bin/uv"
    [ -x "$UV" ] || UV="$(command -v uv)"
    "$UV" tool install --force --python ">=3.11" "$SPEC"
  else
    say "Install uv or pipx, then run this again."
    exit 1
  fi
fi

TABNAP="$(command -v tabnap || true)"
[ -n "$TABNAP" ] || TABNAP="$HOME/.local/bin/tabnap"
if [ ! -x "$TABNAP" ]; then
  say "Installed, but tabnap isn't on your PATH yet. Add ~/.local/bin to PATH, then run: tabnap install"
  exit 0
fi
say ""
"$TABNAP" install </dev/tty
