#!/usr/bin/env bash

set -o errexit
set -o pipefail
set -o xtrace

# This script is meant to be run with tini -g, which forwards signals
# child processes. This enables the use of `trap`
# for cleanup. We don't want to use `exec` for any commands that
# we might want to kill with TERM or clean up after.

trap cleanup HUP INT QUIT TERM

CELERY_WORKER=syncworker1
CELERY_PID_FILE=${WPTSYNC_ROOT}/%n.pid
CELERY_LOG_FILE=${WPTSYNC_ROOT}/logs/%n%I.log
CELERYBEAT_PID_FILE=${WPTSYNC_ROOT}/celerybeat.pid

cleanup() {
    echo "Stopping celery..."
    /app/venv/bin/celery multi stopwait ${CELERY_WORKER} \
        --pidfile=${CELERY_PID_FILE} \
        --logfile=${CELERY_LOG_FILE}
    echo -n "Stopping celery beat..."
    if [ -f "$CELERYBEAT_PID_FILE" ]; then
       kill -0 $(cat "$CELERYBEAT_PID_FILE") 1>/dev/null 2>&1
       if [ $? -eq 1 ]; then
            echo "Already stopped."
            rm $CELERYBEAT_PID_FILE
       else
            kill -TERM $(cat "$CELERYBEAT_PID_FILE")
       fi
    else
        echo "celery beat not running? (no pid file)"
    fi
    sudo service rabbitmq-server stop
}

clean_pid() {
    pidfile=$1
    if [ -f $pidfile ]; then
        kill -0 $(cat "$pidfile") 1>/dev/null 2>&1
        if [ $? -eq 1 ]; then
            echo "Removing stale pid file: $pidfile"
            rm $pidfile
        else
            echo "Process for $pidfile is running!"
            exit 1
        fi
    fi
}

clean_pid "${WPTSYNC_ROOT}/${CELERY_WORKER}.pid"
clean_pid "$CELERYBEAT_PID_FILE"

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
    if [ "$1" != "--shell" ]; then
        /app/venv/bin/wptsync repo-config web-platform-tests ${WPTSYNC_WPT_CONFIG:-/app/wpt-sync/docker/wpt_config}
        /app/venv/bin/wptsync repo-config gecko ${WPTSYNC_GECKO_CONFIG:-/app/wpt-sync/docker/gecko_config}
    fi
fi

env

if [ "$1" == "--shell" ]; then
    bash
elif [ "$1" == "--worker" ]; then
    service --status-all
    sudo service rabbitmq-server start
    sudo service rabbitmq-server status

    echo "Starting celerybeat"

    set +x
    export NEW_RELIC_LICENSE_KEY=$(/app/get_ini.py /app/workspace/credentials.ini newrelic license_key)
    set -x
    export NEW_RELIC_CONFIG_FILE=/app/workspace/newrelic.ini

    newrelic-admin run-program \
                   /app/venv/bin/celery beat --detach --app sync.worker \
                   --schedule=${WPTSYNC_ROOT}/celerybeat-schedule \
                   --pidfile=${CELERYBEAT_PID_FILE} \
                   --logfile=${WPTSYNC_ROOT}/logs/celerybeat.log --loglevel=DEBUG

    echo "Starting celery worker"

    newrelic-admin run-program \
                   /app/venv/bin/celery multi start ${CELERY_WORKER} -A sync.worker \
                   --concurrency=1 \
                   --pidfile=${CELERY_PID_FILE} \
                   --logfile=${CELERY_LOG_FILE} --loglevel=DEBUG

    echo "Starting pulse listener"

    newrelic-admin run-program \
         /app/venv/bin/wptsync listen
elif [ "$1" == "--test" ]; then
    command="test"
    if [ "$2" == "--no-flake8" ]; then
        command="$command --no-flake8"
    fi
    /app/venv/bin/wptsync $command
else
    /app/venv/bin/wptsync "$@"
fi
