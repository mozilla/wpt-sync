FROM python:2.7-stretch

RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid 10001 --home /app --create-home app

COPY ./wpt-sync/docker/start_wptsync.sh /app/start_wptsync.sh
RUN chmod +x /app/start_wptsync.sh

# install/update git, rabbitmq and dependencies
RUN set -ex; \
    apt-get update; \
    apt-get install -y erlang rabbitmq-server git sudo

RUN adduser app sudo
RUN echo '%sudo ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

# get logs to stdout
ENV RABBITMQ_LOGS=- RABBITMQ_SASL_LOGS=-

COPY . /app/vct
RUN chown -R app:app /app/vct

USER app

# Set up files and config
RUN mkdir /app/workspace \
    /app/workspace/logs \
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

WORKDIR /app/vct/wpt-sync

# Copy test credentials as a temporary placeholder
RUN cp /app/vct/wpt-sync/test/testdata/credentials.ini /app/workspace/credentials.ini \
    && cp /app/vct/wpt-sync/test/testdata/sync.ini /app/workspace/sync.ini

RUN cp /app/vct/wpt-sync/docker/known_hosts /app/.ssh/known_hosts

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
# /app/workspace: bind mount to [empty] dir where service will write working data, logs
# config files, credentials, ssh config
# /app/repos: bind mount to ebs volume for gecko and wpt repos (or local dev repos)
VOLUME ["/app/vct", "/app/workspace", "/app/repos"]

ENTRYPOINT ["/app/start_wptsync.sh"]
CMD ["--worker"]
