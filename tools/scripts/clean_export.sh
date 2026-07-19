#!/usr/bin/env sh
set -eu
SRC="${1:-.}"
DST="${2:-}"
if [ -z "$DST" ]; then
  echo "Usage: $0 <source_dir> <dest_dir>" >&2
  exit 1
fi
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
python "$SCRIPT_DIR/export_hardening.py" clean "$SRC" "$DST"
