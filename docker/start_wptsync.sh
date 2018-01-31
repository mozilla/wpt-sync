#!/usr/bin/env bash

set -o errexit
set -o pipefail
set -o xtrace

# USE the trap if you need to also do manual cleanup after the service is stopped,
#     or need to start multiple services in the one container
trap "echo TRAPed signal" HUP INT QUIT TERM

echo Args: $@

cp -v ${WPTSYNC_CONFIG:-/app/wpt-sync/sync.ini} /app/workspace/sync.ini
cp -v ${WPTSYNC_NEW_RELIC_CONFIG:-/app/wpt-sync/newrelic.ini} /app/workspace/newrelic.ini

if [ "$1" != "--test" ]; then
    eval "$(ssh-agent -s)"
    cp -v ${WPTSYNC_SSH_CONFIG:-/app/wpt-sync/docker/ssh_config} /app/.ssh/config
    # Install ssh keys
    cp -v ${WPTSYNC_GH_SSH_KEY:-/app/workspace/ssh/id_github} /app/.ssh/id_github
    cp -v ${WPTSYNC_HGMO_SSH_KEY:-/app/workspace/ssh/id_hgmo} /app/.ssh/id_hgmo
    ssh-add /app/.ssh/id_github
    ssh-add /app/.ssh/id_hgmo
    if [ -n "$WPTSYNC_CREDS" ]; then
        cp -v $WPTSYNC_CREDS /app/workspace/credentials.ini
    fi

    /app/venv/bin/wptsync repo-config web-platform-tests ${WPTSYNC_WPT_CONFIG:-/app/wpt-sync/docker/wpt_config}
    /app/venv/bin/wptsync repo-config gecko $FILE ${WPTSYNC_GECKO_CONFIG:-/app/wpt-sync/docker/gecko_config}
fi

env

if [ "$1" == "--shell" ]; then
    bash
elif [ "$1" == "--worker" ]; then
    service --status-all
    sudo service rabbitmq-server start
    sudo service rabbitmq-server status

    echo "Starting celerybeat"

    export NEW_RELIC_LICENSE_KEY=$(/app/get_ini.py /app/workspace/credentials.ini newrelic license_key)
    export NEW_RELIC_CONFIG_FILE=/app/workspace/newrelic.ini

    newrelic-admin run-program \
                   /app/venv/bin/celery beat --detach --app sync.worker \
                   --schedule=${WPTSYNC_ROOT}/celerybeat-schedule \
                   --pidfile=${WPTSYNC_ROOT}/celerybeat.pid \
                   --logfile=${WPTSYNC_ROOT}/logs/celerybeat.log --loglevel=DEBUG

    echo "Starting celery worker"

    newrelic-admin run-program \
                   /app/venv/bin/celery multi start syncworker1 -A sync.worker \
                   --concurrency=1 \
                   --pidfile=${WPTSYNC_ROOT}/%n.pid \
                   --logfile=${WPTSYNC_ROOT}/logs/%n%I.log --loglevel=DEBUG

    echo "Starting pulse listener"

    exec newrelic-admin run-program \
         /app/venv/bin/wptsync listen
elif [ "$1" == "--test" ]; then
    exec /app/venv/bin/wptsync test
else
    exec /app/venv/bin/wptsync "$@"
fi
