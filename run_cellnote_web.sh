#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${CELLNOTE_WEB_ENV:-$ROOT/configs/web.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi
export PYTHONPATH="$ROOT/.webdeps:$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export CELLNOTE_REPO_ROOT="$ROOT"
exec /home/lixinyu/miniforge3/envs/cellnote-agent/bin/python -m uvicorn cell_note_agent.web.app:app --host 127.0.0.1 --port "${CELLNOTE_WEB_PORT:-8787}"
