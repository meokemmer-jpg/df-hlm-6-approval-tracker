#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_DIR="${DF_HLM_6_LOCK_DIR:-/tmp/df-hlm-6.lock}"
ENGINE_PATTERN="${ROOT_DIR}/src/approval_tracker.py"

if [[ -e "${LOCK_DIR}" ]]; then
  echo "DF-HLM-6 mutex busy: ${LOCK_DIR}" >&2
  exit 16
fi

mkdir -p "${LOCK_DIR}"
trap 'rmdir "${LOCK_DIR}" >/dev/null 2>&1 || true' EXIT

if command -v pgrep >/dev/null 2>&1; then
  SELF_PID="$$"
  if pgrep -f "${ENGINE_PATTERN}" | grep -v "^${SELF_PID}\$" >/dev/null 2>&1; then
    echo "DF-HLM-6 engine already running: ${ENGINE_PATTERN}" >&2
    exit 17
  fi
fi

exec python3 "${ROOT_DIR}/src/approval_tracker.py" --runtime-dir "${ROOT_DIR}/runtime" "$@"
