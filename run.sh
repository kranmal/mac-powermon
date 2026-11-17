#!/usr/bin/env bash
# Start Mac Powermon in the background (detached from the terminal).
here="$(cd "$(dirname "$0")" && pwd)"
nohup "$here/.venv/bin/python" "$here/powermon.py" "$@" >/dev/null 2>&1 &
