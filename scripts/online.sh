#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
exec "${CONDA_EXE:-conda}" run --no-capture-output -n feishu-api python -m wiki_review_v2.online "$@"
