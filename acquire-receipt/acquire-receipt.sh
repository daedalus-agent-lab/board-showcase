#!/bin/sh
# Thin wrapper: the tool is python3-only. Kept so existing citations of acquire-receipt.sh work.
DIR=$(cd "$(dirname "$0")" && pwd)
exec python3 "$DIR/acquire-receipt.py" "$@"
