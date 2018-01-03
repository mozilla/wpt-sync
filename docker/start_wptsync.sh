#!/usr/bin/env bash

set -o errexit
set -o pipefail
set -o nounset
set -o xtrace

# USE the trap if you need to also do manual cleanup after the service is stopped,
#     or need to start multiple services in the one container
trap "echo TRAPed signal" HUP INT QUIT TERM

#cp -v ${WPTSYNC_CONFIG:-/app/vct/wpt-sync/sync.ini} /app/workspace/sync.ini
#cp -v ${WPTSYNC_CREDS:-/app/data/credentials.ini} /app/workspace/credentials.ini

# Install ssh keys
#echo ${WPTSYNC_GH_SSH_KEY} > /app/.ssh/id_github
#echo ${WPTSYNC_HGMO_SSH_KEY} > /app/.ssh/id_hgmo

git --version
hg --version

eval "$(ssh-agent -s)"

service --status-all
sudo service rabbitmq-server start
sudo service rabbitmq-server status

ls -lh /app/workspace/logs

if [ "$1" == "--worker" ]; then
    echo "Starting celerybeat"

    /app/venv/bin/celery beat --detach --app sync.worker \
                         --pidfile=${WPTSYNC_ROOT}/celerybeat.pid \
                         --logfile=${WPTSYNC_ROOT}/celerybeat.log --loglevel=DEBUG

    echo "Starting celery worker"

    /app/venv/bin/celery multi start syncworker1 -A sync.worker \
                         --pidfile=${WPTSYNC_ROOT}/%n.pid \
                         --logfile=${WPTSYNC_ROOT}/%n%I.log --loglevel=DEBUG

    echo "Starting pulse listener"

    exec /app/venv/bin/wptsync listen
fi
