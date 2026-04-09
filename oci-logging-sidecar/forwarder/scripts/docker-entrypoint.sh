#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '[entrypoint] %s\n' "$*"
}

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    log "missing required environment variable: ${name}"
    exit 1
  fi
}

render_template() {
  local src="$1"
  local dst="$2"

  python3 - "${src}" "${dst}" <<'PY'
import os
import re
import sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])
template = src.read_text(encoding="utf-8")
rendered = re.sub(r"\$\{([A-Z0-9_]+)\}", lambda match: os.environ.get(match.group(1), ""), template)
dst.write_text(rendered, encoding="utf-8")
PY
}

start_logrotate_loop() {
  while true; do
    /usr/sbin/logrotate -v -s "${LOGROTATE_STATE_FILE}" /etc/logrotate.d/log-file.conf
    sleep "${LOGROTATE_INTERVAL_SECONDS}"
  done
}

main() {
  require_env LOG_FILE_PATH
  require_env OCI_LOG_OBJECT_ID

  export LOG_FORWARDER_LOG_LEVEL="${LOG_FORWARDER_LOG_LEVEL:-INFO}"
  export OCI_LOG_TYPE="${OCI_LOG_TYPE:-app.log}"
  export READ_FROM_HEAD="${READ_FROM_HEAD:-true}"
  export LOG_FORWARDER_FLUSH_INTERVAL="${LOG_FORWARDER_FLUSH_INTERVAL:-5s}"
  export LOG_FORWARDER_CHUNK_LIMIT_SIZE="${LOG_FORWARDER_CHUNK_LIMIT_SIZE:-1m}"
  export LOG_FORWARDER_QUEUED_BATCH_LIMIT="${LOG_FORWARDER_QUEUED_BATCH_LIMIT:-64}"
  export LOG_FORWARDER_DISK_USAGE_LOG_INTERVAL="${LOG_FORWARDER_DISK_USAGE_LOG_INTERVAL:-5m}"
  export LOGROTATE_FREQUENCY="${LOGROTATE_FREQUENCY:-hourly}"
  export LOGROTATE_ROTATE_COUNT="${LOGROTATE_ROTATE_COUNT:-24}"
  export LOGROTATE_SIZE="${LOGROTATE_SIZE:-50M}"

  if [[ -n "${OCI_AUTH_TYPE:-}" && "${OCI_AUTH_TYPE}" != "resource_principal" ]]; then
    log "unsupported OCI_AUTH_TYPE=${OCI_AUTH_TYPE}; this image only supports resource_principal"
    exit 1
  fi
  export OCI_AUTH_TYPE="resource_principal"

  if [[ ! -f "${LOG_FILE_PATH}" ]]; then
    log "creating missing log file ${LOG_FILE_PATH}"
    mkdir -p "$(dirname "${LOG_FILE_PATH}")"
    touch "${LOG_FILE_PATH}"
  fi

  mkdir -p \
    "$(dirname "${LOGROTATE_STATE_FILE}")" \
    "${LOG_FORWARDER_SPOOL_DIR}" \
    "${LOG_FORWARDER_STATE_DIR}"

  render_template /etc/logrotate.d/log-file.conf.template /etc/logrotate.d/log-file.conf

  log "starting OCI log forwarder"
  log "source file: ${LOG_FILE_PATH}"
  log "OCI auth mode: resource_principal"
  log "OCI log object id: ${OCI_LOG_OBJECT_ID}"

  start_logrotate_loop &
  logrotate_pid="$!"
  python3 -u /opt/oci-log-forwarder/oci_log_forwarder.py &
  forwarder_pid="$!"

  cleanup() {
    kill "${forwarder_pid}" 2>/dev/null || true
    kill "${logrotate_pid}" 2>/dev/null || true
    wait "${forwarder_pid}" 2>/dev/null || true
    wait "${logrotate_pid}" 2>/dev/null || true
  }
  trap cleanup EXIT INT TERM

  wait -n "${forwarder_pid}" "${logrotate_pid}"
  exit_code="$?"
  cleanup
  exit "${exit_code}"
}

main "$@"
