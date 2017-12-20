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
echo ${WPTSYNC_GH_SSH_KEY} > /app/.ssh/id_github
echo ${WPTSYNC_HGMO_SSH_KEY} > /app/.ssh/id_hgmo

git --version
hg --version

export SHELL=/bin/bash
eval "$(ssh-agent -s)"
if [ -e /app/ssh/id_github_ecdsa ]; then
    ssh-add /app/ssh/id_github_ecdsa
fi
if [ -e /app/ssh/id_mozilla_ecdsa ]; then
    ssh-add /app/ssh/id_mozilla_ecdsa
fi
mkdir -p ~/.ssh
ssh-keyscan -H github.com >> ~/.ssh/known_hosts
ssh-keyscan -H hg.mozilla.org >> ~/.ssh/known_hosts
echo "Host hg.mozilla.org
User james@hoppipolla.co.uk
IdentityFile /app/ssh/id_mozilla_ecdsa

Host github.com
IdentityFile /app/ssh/id_github_ecdsa
" >> ~/.ssh/config

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
