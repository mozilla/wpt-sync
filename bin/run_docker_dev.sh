#!/bin/bash

# Helper script for running docker in a dev environment. 
# Use run_docker.sh instead in prod

if [[ $1 == "build" ]]; then
    docker build -t wptsync_dev --file docker/Dockerfile.dev .
elif [[ $1 == "test" ]]; then
    exec docker run -it wptsync_dev --test
else
    exec docker run --init -it --add-host=rabbitmq:127.0.0.1 \
    --env WPTSYNC_CONFIG=/app/wpt-sync/devenv/sync_maja.ini \
    --env WPTSYNC_CREDS=/app/wpt-sync/devenv/credentials_maja.ini \
    --env WPTSYNC_SSH_CONFIG=/app/wpt-sync/devenv/ssh_config \
    --env WPTSYNC_GECKO_CONFIG=/app/wpt-sync/devenv/gecko_config \
    --env WPTSYNC_WPT_CONFIG=/app/wpt-sync/devenv/wpt_config \
    --mount type=bind,source=$(pwd),target=/app/wpt-sync \
    --mount type=bind,source=$(pwd)/repos,target=/app/repos \
    --mount type=bind,source=$(pwd)/workspace,target=/app/workspace wptsync_dev $@
fi
