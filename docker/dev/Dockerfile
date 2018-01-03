FROM python:2.7-stretch

RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid 10001 --home /app --create-home app

ADD ./wpt-sync/docker/start_wptsync.sh /app/start_wptsync.sh
RUN chmod +x /app/start_wptsync.sh

# install/update git, rabbitmq and dependencies
RUN set -ex; \
    apt-get update; \
    apt-get install -y erlang rabbitmq-server git sudo

RUN adduser app sudo
RUN echo '%sudo ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

# get logs to stdout
ENV RABBITMQ_LOGS=- RABBITMQ_SASL_LOGS=-

USER app

# Set up files and config
RUN mkdir /app/workspace \
    /app/vct \
    /app/data \
    /app/repos \
    /app/git-cinnabar \
    /app/.ssh

# Install git-cinnabar
WORKDIR /app/git-cinnabar

RUN git clone https://github.com/glandium/git-cinnabar.git . \
    && git checkout bb45a922e9c7907ad6e101fbaa54cec5cb7b2434

ENV PATH=/app/git-cinnabar:$PATH

RUN pip install --user requests

RUN git cinnabar download

WORKDIR /app/vct

RUN git config --global user.name moz-wptsync-bot \
    && git config --global user.email wptsync@mozilla.com

# Copy the current directory contents into the container
ADD . /app/vct

WORKDIR /app/vct/wpt-sync

# Seed default configuration files
# Used only for running tests
RUN cp /app/vct/wpt-sync/test_credentials.ini /app/workspace/credentials.ini

RUN cp /app/vct/wpt-sync/sync.ini /app/workspace/sync.ini \
    && cp /app/vct/wpt-sync/docker/known_hosts /app/.ssh/known_hosts

# TODO make prod version that installs wheels?
# Install the app in development mode
RUN virtualenv /app/venv \
    && /app/venv/bin/pip install -r prod-requirements.txt --no-deps --require-hashes \
    && /app/venv/bin/pip install -r requirements-dev.txt --no-deps \
    && /app/venv/bin/pip install ../pylib/mozautomation --no-deps

WORKDIR /app

# This puts the egg-info dir in /app, which won't be overwritten
# when docker mounts the volume for our source code
RUN /app/venv/bin/python /app/vct/wpt-sync/setup.py develop --no-deps

ENV PYTHONPATH=/app/vct/wpt-sync
ENV WPTSYNC_ROOT=/app/workspace
ENV WPTSYNC_REPO_ROOT=/app/repos
# Allow git-cinnabar to find latest hg
ENV PATH=/app/venv/bin:$PATH

# Mach requires that this is set
ENV SHELL=/bin/bash

WORKDIR /app/vct/wpt-sync

# /app/vct: bind mount to src dir (only on dev) or dir with wheels?
# /app/workspace: docker volume for logs, worktrees, etc.
# /app/repos: bind mount to ebs volume for gecko and wpt repos
# /app/data: bind bound to host data such as config files, credentials
VOLUME ["/app/vct", "/app/workspace", "/app/repos", "/app/data"]

ENTRYPOINT ["/app/start_wptsync.sh"]
CMD ["--worker"]
