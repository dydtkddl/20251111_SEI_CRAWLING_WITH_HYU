#!/bin/sh
set -eu

err() { printf "%s\n" "$*" >&2; }

abspath() {
  _p="$1"
  case "$_p" in
    /*) _cand="$_p" ;;
    *)  _cand="${CALL_PWD}/$_p" ;;
  esac

  if command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY' "$_cand"
import os, sys
print(os.path.abspath(sys.argv[1]))
PY
    return 0
  fi

  (cd "$_cand" 2>/dev/null && pwd) || true
}

CALL_PWD="$(pwd)"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

DO_BUILD=0
if [ "${1:-}" = "--build" ]; then
  DO_BUILD=1
  shift
fi

if [ "${1:-}" = "" ]; then
  TARGET_DIR="$CALL_PWD"
else
  TARGET_DIR="$1"
fi

WORKSPACE_DIR="$(abspath "$TARGET_DIR")"
if [ -z "${WORKSPACE_DIR:-}" ] || [ ! -d "$WORKSPACE_DIR" ]; then
  err "[FATAL] Workspace directory not found: $WORKSPACE_DIR"
  exit 1
fi

HF_CACHE_DIR="${HF_CACHE_DIR:-$HOME/.cache/huggingface}"
mkdir -p "$HF_CACHE_DIR" 2>/dev/null || true

COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-rtx5090}"
export WORKSPACE_DIR HF_CACHE_DIR COMPOSE_PROJECT_NAME

cd "$SCRIPT_DIR"

printf "%s\n" "[INFO] Compose dir         : $SCRIPT_DIR"
printf "%s\n" "[INFO] WORKSPACE_DIR       : $WORKSPACE_DIR"
printf "%s\n" "[INFO] HF_CACHE_DIR        : $HF_CACHE_DIR"
printf "%s\n" "[INFO] COMPOSE_PROJECT_NAME: $COMPOSE_PROJECT_NAME"

if [ "$DO_BUILD" -eq 1 ]; then
  docker compose up -d --build
else
  docker compose up -d
fi

if [ -t 0 ] && [ -t 1 ]; then
  docker compose exec -it blackwell-env bash
else
  docker compose exec -i blackwell-env bash
fi

