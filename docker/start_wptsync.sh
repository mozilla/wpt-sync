#!/usr/bin/env bash

set -o errexit
set -o pipefail
set -o nounset
set -o xtrace

# USE the trap if you need to also do manual cleanup after the service is stopped,
#     or need to start multiple services in the one container
trap "echo TRAPed signal" HUP INT QUIT TERM

echo Args: $@

if [ "$1" != "--test" ]; then
    eval "$(ssh-agent -s)"
    cp -v ${WPTSYNC_CONFIG:-/app/vct/wpt-sync/sync.ini} /app/workspace/sync.ini
    cp -v ${WPTSYNC_CREDS:-/app/data/credentials.ini} /app/workspace/credentials.ini
    cp -v ${WPTSYNC_SSH_CONFIG:-/app/vct/wpt-sync/docker/ssh_config} /app/.ssh/config
    # Install ssh keys
    cp -v ${WPTSYNC_GH_SSH_KEY:-/app/data/ssh/id_github} /app/.ssh/id_github
    cp -v ${WPTSYNC_HGMO_SSH_KEY:-/app/data/ssh/id_hgmo} /app/.ssh/id_hgmo
    ssh-add /app/.ssh/id_github
    ssh-add /app/.ssh/id_hgmo
fi

git --version
hg --version

env

service --status-all
sudo service rabbitmq-server start
sudo service rabbitmq-server status

# if we've never run wptsync service, log dir may not exist
mkdir -p /app/workspace/logs
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
elif [ "$1" == "--test" ]; then
    exec /app/venv/bin/wptsync test
else
    exec /app/venv/bin/wptsync "$@"
fi
