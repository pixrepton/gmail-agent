#!/usr/bin/env sh
set -eu
DST="${1:?usage: verify_export_clean.sh <export_dir>}"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
python "$SCRIPT_DIR/export_hardening.py" verify "$DST"
