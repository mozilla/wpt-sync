#!/bin/bash

set -eo pipefail

# Helper script for running docker in a dev environment.
# Use run_docker.sh instead in prod

command=$1
shift

DEFAULT_TAG=wptsync_dev_py3;
if [[ "$1" == "--py3" ]]; then
    # Old argument for backwards compat
    shift
fi

DOCKER_TAG=${DOCKER_TAG:-$DEFAULT_TAG}

if [[ $command == "build" ]]; then
    if [ "$1" == "--test" ]; then
        TEST=1
    else
        TEST=0
    fi
    if [[ $TEST == 0 && ! -d $(pwd)/config/dev/ssh ]]; then
        echo "Creating development credentials for Github";
        mkdir -p $(pwd)/config/dev/ssh
        ssh-keygen -f $(pwd)/config/dev/ssh/id_github -t rsa -b 4096
        echo "Creating development credentials for hg.m.o"
        ssh-keygen -f $(pwd)/config/dev/ssh/id_hgmo -t rsa -b 4096 -C wptsync@mozilla.com
    fi
    if [[ $TEST == 0 && ! -d $(pwd)/workspace ]]; then
        mkdir -p $(pwd)/workspace;
        mkdir -p $(pwd)/workspace/locks;
        mkdir -p $(pwd)/workspace/logs;
        mkdir -p $(pwd)/workspace/logs/rabbitmq;
        mkdir -p $(pwd)/workspace/work;
    fi
    if [[ $TEST == 0 && ! -d $(pwd)/repos ]]; then
        echo "Creating repos directory"
        mkdir $(pwd)/repos
        mkdir $(pwd)/repos/gecko
        cd $(pwd)/repos/gecko
        git init
        cd -
        mkdir $(pwd)/repos/web-platform-tests
        cd $(pwd)/repos/web-platform-tests
        git init
        cd -
    fi
    docker build -t ${DOCKER_TAG} --file docker/Dockerfile.dev .
    echo "you may need to run:
sudo chown -R 10001:10001 config/dev workspace repos
sudo chown -R 10002:10002 workspace/logs/rabbitmq"
elif [[ $command == "test" ]]; then
    exec docker run \
         --env WPTSYNC_CONFIG=/app/config/test/sync.ini \
         --env WPTSYNC_CREDS=/app/config/test/credentials.ini \
         --mount type=bind,source=$(pwd)/sync,target=/app/wpt-sync/sync \
         --mount type=bind,source=$(pwd)/test/config,target=/app/config/test \
         --mount type=bind,source=$(pwd)/test,target=/app/wpt-sync/test \
         ${DOCKER_TAG} --test $@
elif [[ $command == "run" ]]; then
    exec docker run -it --add-host=rabbitmq:127.0.0.1 \
    --env WPTSYNC_CONFIG=${WPTSYNC_CONFIG:-/app/config/dev/sync.ini} \
    --env WPTSYNC_CREDS=${WPTSYNC_CREDS:-/app/config/dev/credentials.ini} \
    --env WPTSYNC_GECKO_CONFIG=${WPTSYNC_GECKO_CONFIG:-/app/config/gecko_config} \
    --env WPTSYNC_WPT_CONFIG=${WPTSYNC_WPT_CONFIG:-/app/config/wpt_config} \
    --env WPTSYNC_WPT_METADATA_CONFIG=${WPTSYNC_WPT_METADATA_CONFIG:-/app/config/wpt-metadata_config} \
    --env WPTSYNC_GH_SSH_KEY=${WPTSYNC_GH_SSH_KEY:-/app/config/dev/ssh/id_github} \
    --env WPTSYNC_HGMO_SSH_KEY=${WPTSYNC_HGMO_SSH_KEY:-/app/config/dev/ssh/id_hgmo} \
    --mount type=bind,source=$(pwd)/config,target=/app/config \
    --mount type=bind,source=$(pwd)/sync,target=/app/wpt-sync/sync \
    --mount type=bind,source=$(pwd)/repos,target=/app/repos \
    --mount type=bind,source=$(pwd)/workspace,target=/app/workspace \
    ${DOCKER_TAG} $@
else
  echo "Usage: $0 build|test|run [optional args to for <run>...]"
fi
