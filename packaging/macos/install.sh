#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
INSTALL_ROOT="${GITGO_MACOS_INSTALL_ROOT:-$HOME/Library/Application Support/Gitgo/app}"
BIN_DIR="${GITGO_MACOS_BIN_DIR:-$HOME/.local/bin}"
UPDATE_PATH=1
START_MARKER="# >>> Gitgo managed PATH >>>"
END_MARKER="# <<< Gitgo managed PATH <<<"

usage() {
  cat <<'EOF'
Usage: ./install.sh [--install-root DIR] [--bin-dir DIR] [--no-path-update]
Installs Gitgo for the current macOS user. Administrator access is not needed.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --install-root) INSTALL_ROOT="$2"; shift 2 ;;
    --bin-dir) BIN_DIR="$2"; shift 2 ;;
    --no-path-update) UPDATE_PATH=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$INSTALL_ROOT" in
  /*) ;;
  *) echo "Install root must be an absolute path: $INSTALL_ROOT" >&2; exit 1 ;;
esac
case "/$INSTALL_ROOT/" in */../*|*/./*) echo "Unsafe install root: $INSTALL_ROOT" >&2; exit 1 ;; esac
case "$INSTALL_ROOT" in ""|/|"$HOME") echo "Unsafe install root: $INSTALL_ROOT" >&2; exit 1 ;; esac
case "$BIN_DIR" in
  /*) ;;
  *) echo "Command directory must be an absolute path: $BIN_DIR" >&2; exit 1 ;;
esac
case "/$BIN_DIR/" in */../*|*/./*) echo "Unsafe command directory: $BIN_DIR" >&2; exit 1 ;; esac
case "$BIN_DIR" in ""|/) echo "Unsafe command directory: $BIN_DIR" >&2; exit 1 ;; esac
if [ "$SCRIPT_DIR" = "$INSTALL_ROOT" ]; then
  echo "Run install.sh from an extracted release, not the installed directory" >&2
  exit 1
fi
if [ ! -f "$SCRIPT_DIR/product.json" ]; then echo "Missing product.json" >&2; exit 1; fi

PLATFORM="$(/usr/bin/plutil -extract platform raw -o - "$SCRIPT_DIR/product.json")"
ARCHITECTURE="$(/usr/bin/plutil -extract architecture raw -o - "$SCRIPT_DIR/product.json")"
COMMAND="$(/usr/bin/plutil -extract primary_command raw -o - "$SCRIPT_DIR/product.json")"
case "$COMMAND" in ""|*[!A-Za-z0-9._-]*) echo "Unsafe product command: $COMMAND" >&2; exit 1 ;; esac
if [ "$PLATFORM" != "macos" ]; then echo "This is not a macOS release" >&2; exit 1; fi
if [ "$ARCHITECTURE" != "$(uname -m)" ]; then
  echo "Package architecture $ARCHITECTURE does not match this Mac ($(uname -m))" >&2
  exit 1
fi
if [ ! -x "$SCRIPT_DIR/$COMMAND" ] || [ ! -x "$SCRIPT_DIR/internal/gitgo-host/gitgo-host" ]; then
  echo "Release payload is incomplete" >&2
  exit 1
fi

INSTALL_PARENT="$(dirname "$INSTALL_ROOT")"
STAGING="$INSTALL_PARENT/.gitgo-installing-$$"
BACKUP="$INSTALL_PARENT/.gitgo-backup-$$"
case "$STAGING" in /|"$HOME") echo "Unsafe staging directory" >&2; exit 1 ;; esac

rollback() {
  status=$?
  if [ "$status" -ne 0 ]; then
    rm -rf "$STAGING"
    if [ -e "$BACKUP" ]; then
      rm -rf "$INSTALL_ROOT"
      mv "$BACKUP" "$INSTALL_ROOT"
    fi
  fi
  exit "$status"
}
trap rollback EXIT INT TERM

mkdir -p "$INSTALL_PARENT" "$BIN_DIR"
rm -rf "$STAGING" "$BACKUP"
/usr/bin/ditto "$SCRIPT_DIR" "$STAGING"
if [ -e "$INSTALL_ROOT" ]; then mv "$INSTALL_ROOT" "$BACKUP"; fi
mv "$STAGING" "$INSTALL_ROOT"

link_command() {
  name="$1"
  link="$BIN_DIR/$name"
  target="$INSTALL_ROOT/$name"
  if [ -e "$link" ] || [ -L "$link" ]; then
    if [ ! -L "$link" ] || [ "$(readlink "$link")" != "$target" ]; then
      echo "Refusing to replace command not owned by Gitgo: $link" >&2
      return 1
    fi
    rm "$link"
  fi
  ln -s "$target" "$link"
}
link_command "$COMMAND"

index=0
while alias="$(/usr/bin/plutil -extract "command_aliases.$index" raw -o - "$INSTALL_ROOT/product.json" 2>/dev/null)"; do
  [ -n "$alias" ] && link_command "$alias"
  index=$((index + 1))
done

append_posix_path() {
  profile="$1"
  mkdir -p "$(dirname "$profile")"
  touch "$profile"
  if /usr/bin/grep -Fq "$START_MARKER" "$profile"; then return; fi
  quoted_bin="$(printf '%q' "$BIN_DIR")"
  {
    printf '\n%s\n' "$START_MARKER"
    printf 'export PATH=%s:"$PATH"\n' "$quoted_bin"
    printf '%s\n' "$END_MARKER"
  } >> "$profile"
}

if [ "$UPDATE_PATH" -eq 1 ]; then
  case "$(basename "${SHELL:-}")" in
    zsh) append_posix_path "$HOME/.zprofile" ;;
    bash) append_posix_path "$HOME/.bash_profile" ;;
    fish)
      if [ "$BIN_DIR" = "$HOME/.local/bin" ]; then
        fish_config="$HOME/.config/fish/conf.d/gitgo.fish"
        mkdir -p "$(dirname "$fish_config")"
        cat > "$fish_config" <<'EOF'
# >>> Gitgo managed PATH >>>
fish_add_path --path "$HOME/.local/bin"
# <<< Gitgo managed PATH <<<
EOF
      else
        echo "Custom --bin-dir selected; add it to fish PATH manually: $BIN_DIR" >&2
      fi
      ;;
    *) echo "Add this directory to PATH: $BIN_DIR" >&2 ;;
  esac
fi

rm -rf "$BACKUP"
trap - EXIT INT TERM
echo "Gitgo installed at: $INSTALL_ROOT"
echo "Command: $BIN_DIR/$COMMAND"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "Open a new terminal, or run: export PATH=\"$BIN_DIR:\$PATH\"" ;; esac
