#!/usr/bin/env bash

set -o errexit
set -o pipefail
set -o xtrace

# This script is meant to be run with tini -g, which forwards signals
# child processes. This enables the use of `trap`
# for cleanup. We don't want to use `exec` for any commands that
# we might want to kill with TERM or clean up after.

trap cleanup HUP INT QUIT TERM ERR EXIT

CELERY_WORKER=syncworker1
CELERY_PID_FILE=${WPTSYNC_ROOT}/%n.pid
CELERY_LOG_FILE=${WPTSYNC_ROOT}/logs/%n%I.log
CELERYBEAT_PID_FILE=${WPTSYNC_ROOT}/celerybeat.pid

cleanup() {
    echo "Stopping celery..."
    /app/venv/bin/celery multi stopwait ${CELERY_WORKER} \
        --pidfile=${CELERY_PID_FILE} \
        --logfile=${CELERY_LOG_FILE}
    echo "Stopping celery beat..."
    if [ -f "$CELERYBEAT_PID_FILE" ]; then
       set +e
       kill -0 $(cat "$CELERYBEAT_PID_FILE") 1>/dev/null 2>&1
       result=$?
       set -e
       if [ $result -eq 1 ]; then
            echo "The celerybeat process is not running in this container, but it may be
            running in another. Leaving $CELERYBEAT_PID_FILE in place."
       else
            kill -TERM $(cat "$CELERYBEAT_PID_FILE")
            rm $CELERYBEAT_PID_FILE
       fi
    else
        echo "$CELERYBEAT_PID_FILE does not exist. celerybeat already stopped?"
    fi
    echo "Stopping rabbitmq-server..."
    sudo service rabbitmq-server stop
}

clean_pid() {
    pidfile=$1
    if [ -f $pidfile ]; then
        set +e
        kill -0 $(cat "$pidfile") 1>/dev/null 2>&1
        result=$?
        set -e
        if [ $result -eq 1 ]; then
            echo "The specified process is not running in this container, but it may be
            running in another. Remove $pidfile if it is truly stale and try again."
        else
            echo "Process for $pidfile is running! Stop it and try again."
        fi
        exit 1
    fi
}

if [ "$1" != "--test" ] && [ "$1" != "--shell" ]; then
    eval "$(ssh-agent -s)"
    # Install ssh keys
    cp -v ${WPTSYNC_GH_SSH_KEY:-/app/config/dev/ssh/id_github} /app/.ssh/id_github
    cp -v ${WPTSYNC_HGMO_SSH_KEY:-/app/config/dev/ssh/id_hgmo} /app/.ssh/id_hgmo
    ssh-add /app/.ssh/id_github
    ssh-add /app/.ssh/id_hgmo
    if [ "$1" != "--shell" ]; then
        /app/venv/bin/wptsync repo-config web-platform-tests ${WPTSYNC_WPT_CONFIG:-/app/config/wpt_config}
        /app/venv/bin/wptsync repo-config gecko ${WPTSYNC_GECKO_CONFIG:-/app/config/gecko_config}
    fi
fi

env

if [ "$1" == "--shell" ]; then
    bash
elif [ "$1" == "--worker" ]; then
    clean_pid "${WPTSYNC_ROOT}/${CELERY_WORKER}.pid"
    clean_pid "$CELERYBEAT_PID_FILE"
    service --status-all
    sudo service rabbitmq-server start
    sudo service rabbitmq-server status

    echo "Starting celerybeat"

    set +x
    export NEW_RELIC_LICENSE_KEY=$(/app/get_ini.py /app/config/prod/credentials.ini newrelic license_key)
    set -x
    export NEW_RELIC_CONFIG_FILE=/app/config/newrelic.ini

    # TODO: need to configure the API key correctly to record deploys
    # newrelic-admin record-deploy ${NEW_RELIC_CONFIG_FILE} $(git --git-dir=/app/wpt-sync/.git rev-parse HEAD)

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
    shift 1;
    /app/venv/bin/wptsync test "$@"
else
    /app/venv/bin/wptsync "$@"
fi
