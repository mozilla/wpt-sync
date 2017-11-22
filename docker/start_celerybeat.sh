#!/usr/bin/env bash

set -o errexit
set -o pipefail
set -o nounset
set -o xtrace


rm -f ${CELERYBEAT_PID_FILE}

/home/wptsync/venv/bin/celery beat --app sync.worker \
  --pidfile=${CELERYBEAT_PID_FILE} \
  --logfile=${CELERYBEAT_LOG_FILE} --loglevel=${CELERYBEAT_LOG_LEVEL}
