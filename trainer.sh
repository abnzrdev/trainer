#!/usr/bin/env bash
# filepath: /home/abnzr/projects/trainer/start.sh

set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
PYTHON_BIN="${VENV_DIR}/bin/python"
PIP_BIN="${VENV_DIR}/bin/pip"
REQ_FILE="${PROJECT_DIR}/requirements.txt"
PYPROJECT_FILE="${PROJECT_DIR}/pyproject.toml"

log()  { printf "\033[1;34m[trainer]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[trainer]\033[0m %s\n" "$*" >&2; }
err()  { printf "\033[1;31m[trainer]\033[0m %s\n" "$*" >&2; }

on_error() {
  err "Startup failed at line $1."
  exit 1
}
trap 'on_error $LINENO' ERR

cd "$PROJECT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  err "python3 is required but not found in PATH."
  exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
  log "Creating virtual environment at .venv"
  python3 -m venv "$VENV_DIR"
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  err "Python executable not found in virtual environment: $PYTHON_BIN"
  exit 1
fi

if [[ ! -x "$PIP_BIN" ]]; then
  log "pip not found in virtual environment, bootstrapping pip"
  "$PYTHON_BIN" -m ensurepip --upgrade
fi

if [[ ! -x "$PIP_BIN" ]]; then
  err "pip is unavailable in virtual environment: $PIP_BIN"
  exit 1
fi

if [[ -f "$REQ_FILE" ]]; then
  STAMP_FILE="${VENV_DIR}/.requirements.stamp"
  if [[ ! -f "$STAMP_FILE" || "$REQ_FILE" -nt "$STAMP_FILE" ]]; then
    log "Installing/updating dependencies"
    "$PIP_BIN" install --upgrade pip
    "$PIP_BIN" install -r "$REQ_FILE"
    date -u +"%Y-%m-%dT%H:%M:%SZ" > "$STAMP_FILE"
  else
    log "Dependencies already up to date"
  fi
else
  if [[ -f "$PYPROJECT_FILE" ]]; then
    STAMP_FILE="${VENV_DIR}/.editable-install.stamp"
    if [[ ! -f "$STAMP_FILE" || "$PYPROJECT_FILE" -nt "$STAMP_FILE" ]]; then
      log "requirements.txt missing; installing project dependencies from pyproject.toml"
      "$PIP_BIN" install --upgrade pip
      "$PIP_BIN" install -e "$PROJECT_DIR"
      date -u +"%Y-%m-%dT%H:%M:%SZ" > "$STAMP_FILE"
    else
      log "Editable install already up to date"
    fi
  else
    warn "No requirements.txt or pyproject.toml found; skipping dependency installation"
  fi
fi

export PYTHONPATH="${PROJECT_DIR}/src${PYTHONPATH:+:${PYTHONPATH}}"

log "Starting ICPC Trainer TUI..."
exec "$PYTHON_BIN" -m icpc_trainer.tui "$@"
