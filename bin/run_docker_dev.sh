#!/bin/bash

set -eo pipefail

# Helper script for running docker in a dev environment.
# Use run_docker.sh instead in prod

command=$1
shift

DOCKER_TAG=${DOCKER_TAG:-wptsync_dev}

if [[ $command == "build" ]]; then
    {
      set +e
      echo "Creating devenv"
      mkdir devenv \
          && echo "Copying configs to devenv" \
      && cp test/testdata/* devenv/ \
      echo "Creating workspace"
      mkdir workspace \
      && echo "Creating dev .ssh credentials for workspace" \
      && mkdir workspace/ssh \
      && echo "Creating development credentials for Github" \
      && ssh-keygen -f workspace/ssh/id_github -t rsa -b 4096 \
      && echo "Creating development credentials for hg.m.o" \
      && ssh-keygen -f workspace/ssh/id_hgmo -t rsa -b 4096 -C wptsync@mozilla.com \
      && echo "Creating repos directory" \
      && mkdir repos
    }
    docker build -t wptsync_dev --file docker/Dockerfile.dev .
elif [[ $command == "test" ]]; then
    exec docker run -it \
         --env WPTSYNC_CONFIG=/app/wpt-sync/config/dev/sync.ini \
         --mount type=bind,source=$(pwd),target=/app/wpt-sync \
         wptsync_dev --test $@
elif [[ $command == "run" ]]; then
    exec docker run -it --add-host=rabbitmq:127.0.0.1 \
    --env WPTSYNC_CONFIG=${WPTSYNC_CONFIG:-/app/wpt-sync/config/dev/sync.ini} \
    --env WPTSYNC_CREDS=${WPTSYNC_CREDS:-/app/wpt-sync/config/dev/credentials.ini} \
    --env WPTSYNC_SSH_CONFIG=${WPTSYNC_SSH_CONFIG:-/app/wpt-sync/config/ssh_config} \
    --env WPTSYNC_GECKO_CONFIG=${WPTSYNC_GECKO_CONFIG:/app/wpt-sync/config/gecko_config} \
    --env WPTSYNC_WPT_CONFIG==${WPTSYNC_WPT_CONFIG:/app/wpt-sync/config/wpt_config} \
    --mount type=bind,source=$(pwd)/devenv,target=/app/wpt-sync/devenv \
    --mount type=bind,source=$(pwd)/config,target=/app/wpt-sync/config \
    --mount type=bind,source=$(pwd)/sync,target=/app/wpt-sync/sync \
    --mount type=bind,source=$(pwd)/test,target=/app/wpt-sync/test \
    --mount type=bind,source=$(pwd)/repos,target=/app/repos \
    --mount type=bind,source=$(pwd)/workspace,target=/app/workspace \
    --mount type=bind,source=$(pwd)/workspace/logs/rabbitmq,target=/var/log/rabbitmq \
    $DOCKER_TAG $@
else
  echo "Usage: $0 build|test|run [optional args to for <run>...]"
fi
