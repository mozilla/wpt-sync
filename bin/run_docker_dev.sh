#!/bin/bash

# Helper script for running docker in a dev environment.
# Use run_docker.sh instead in prod

if [[ $1 == "build" ]]; then
    {
      echo "Creating devenv"
      mkdir devenv/
      echo "Copying configs to devenv"
      cp test/testdata/* devenv/
      cp docker/*_config devenv/
      echo "Creating workspace"
      mkdir workspace
      echo "Creating dev .ssh credentials for workspace"
      mkdir workspace/ssh/
      echo "Creating development credentials for Github"
      ssh-keygen -f workspace/ssh/id_github -t rsa -b 4096
      echo "Creating development credentials for hg.m.o"
      ssh-keygen -f workspace/ssh/id_hgmo -t rsa -b 4096 -C wptsync@mozilla.com
      echo "Creating repos directory"
      mkdir repos
    }
    docker build -t wptsync_dev --file docker/Dockerfile.dev .
elif [[ $1 == "test" ]]; then
    exec docker run -it wptsync_dev --test
elif [[ $1 == "clean" ]]; then
    rm -rf workspace
    rm -rf repos
    rm -rf devenv
else
    exec docker run --init -it --add-host=rabbitmq:127.0.0.1 \
    --env WPTSYNC_CONFIG=/app/wpt-sync/devenv/sync.ini \
    --env WPTSYNC_CREDS=/app/wpt-sync/devenv/credentials.ini \
    --env WPTSYNC_SSH_CONFIG=/app/wpt-sync/devenv/ssh_config \
    --env WPTSYNC_GECKO_CONFIG=/app/wpt-sync/devenv/gecko_config \
    --env WPTSYNC_WPT_CONFIG=/app/wpt-sync/devenv/wpt_config \
    --mount type=bind,source=$(pwd),target=/app/wpt-sync \
    --mount type=bind,source=$(pwd)/repos,target=/app/repos \
    --mount type=bind,source=$(pwd)/workspace,target=/app/workspace wptsync_dev $@
fi
