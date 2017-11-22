#!/usr/bin/env bash

set -o errexit
set -o pipefail
set -o nounset
set -o xtrace

/home/wptsync/venv/bin/celery multi start ${CELERY_NODES} -A sync.worker \
  --pidfile=${CELERY_PID_FILE} \
  --logfile=${CELERY_LOG_FILE} --loglevel=${CELERY_LOG_LEVEL}
