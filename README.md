# wpt-sync

[![Build Status](https://travis-ci.org/mozilla/wpt-sync.svg?branch=master)](https://travis-ci.org/mozilla/wpt-sync)

## Description

Synchronize changes between gecko and web-platform-tests

## Additional documentation

See `./docs`

## Development environment

Setting up a development environment
requires [Docker](https://www.docker.com/).

We're _somewhat_ following [mozilla-services' Dockerflow](https://github.com/mozilla-services/Dockerflow).

### Prerequisites

Depending on what command you want to run, (e.g. wptsync listen), you may need
to provide custom configration files at docker run-time by placing them in 
locations that are bind-mounted to the container and setting container environment variables. See `Dockerfile.dev` and `start_wptsync.sh`

You can include these customizations in a shell script or a docker-compose.yml:

*   There's a dev-env convenience script for building the docker image and 
    running the wptsync command in a corresponding container: `./bin/
    run_docker_dev.sh`. It is similar to the script we use in production.
*   Alternately you can use __docker-compose__ (see below) or run docker 
    commands directly.


### Raw docker commands

From repo root:

```
docker build -t wptsync_dev --add-host=rabbitmq:127.0.0.1 --file wpt-sync/docker/Dockerfile.dev .
```

The above sets the repo root as the build context for an image called `wptsync_dev`

To start all the services in the container:

```
docker run --init -it --add-host=rabbitmq:127.0.0.1 \
--env WPTSYNC_CONFIG=/app/path/to/sync.ini \
--env WPTSYNC_CREDS=/app/path/to/credentials.ini \
--env WPTSYNC_SSH_CONFIG=/app/path/to/ssh_config \
--env WPTSYNC_GECKO_CONFIG=/app/path/to/gecko_config\
--env WPTSYNC_WPT_CONFIG=/app/path/to/wpt_config \
--mount type=bind,source=$(pwd),target=/app/wpt-sync \
--mount type=bind,source=$(pwd)/repos,target=/app/repos \
--mount type=bind,source=$(pwd)/workspace,target=/app/workspace \
wptsync_dev
```

This runs the script designated by ENTRYPOINT in the Dockerfile with an `init`process. You could use `--env-file` instead of `--env` to set environment variables in the container.

Stop it with:

```
docker stop [container name]
```

You can see names of running containers with `docker container ls`.

If you want to run a different command in the container
interactively, use the `-it` and `--entrypoint` options like:


```
docker run -it --env WPTSYNC_REPO_ROOT=/app/wpt-sync/test/testdata \
    --mount type=bind,source=$(pwd),target=/app/wpt-sync \
    --entrypoint "some_command" wptsync_dev
```

You can pass additional flags to the entrypoint after the `wptsync_dev` part, like `... --entrypoint "some_command" wptsync_dev -x`

### Volumes to --mount

See the VOLUMES directive in the Dockerfile for information about what
volumes it's expecting.

### Permissions

Inside the Docker container we run as the app user with uid 10001. This user
requires write permissions to directories `repos`, `workspace`, and
`data`.

If you're on Linux, for each path run

```
sudo chown -R 10001 <path>
```

You may not need to do this at all on mac.

### Using docker-compose

The docker-compose.yml file is provided as a convenience in the dev environment and it uses the same Dockerfile referenced in previous instructions.

There are instructions in the docker-compose.yml file about how to customize
your dev environment with appropriate mounts.

From project dir you can run:

```
docker-compose build
```

Then to start the services with the default entrypoint (pulse listener):

```
docker-compose up
```

To start a bash shell that's initialized with the start_wptsync.sh script:

```
docker-compose run sync --shell
```

To run tests:

```
docker-compose run sync --test
```


To run an alternate command, e.g. foo, instead of the default entrypoint:

```
docker-compose run --entrypoint foo sync
```

You can also see an alternate way to run tests without docker-compose in `.travis.yml`.

__Note__ that replacing the default entrypoint means that you're nolonger running the `start_wptsync.sh` script at container start-up and therefore some
configuration may be missing or incomplete. For example, the Dockerfile (build-time) doesn't set up any credentials; instead, credentials are only set up in the container at run-time with the above-mentioned script.
