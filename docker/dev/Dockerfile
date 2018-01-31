FROM python:2.7-stretch

RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid 10001 --home /app --create-home app

COPY ./docker/start_wptsync.sh /app/start_wptsync.sh
RUN chmod +x /app/start_wptsync.sh

COPY ./docker/get_ini.py /app/get_ini.py
RUN chmod +x /app/get_ini.py

# install/update git, rabbitmq and dependencies
RUN set -ex; \
    apt-get update; \
    apt-get install -y erlang rabbitmq-server git sudo less emacs-nox

RUN adduser app sudo
RUN echo '%sudo ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

# get logs to stdout
ENV RABBITMQ_LOGS=- RABBITMQ_SASL_LOGS=-

COPY . /app/wpt-sync
RUN chown -R app:app /app/wpt-sync

USER app

# Set up files and config
RUN mkdir /app/workspace \
    /app/workspace/logs \
    /app/testdata \
    /app/repos \
    /app/git-cinnabar \
    /app/fzf \
    /app/.ssh

# Install git-cinnabar
WORKDIR /app/git-cinnabar

RUN git clone https://github.com/glandium/git-cinnabar.git . \
    && git checkout bb45a922e9c7907ad6e101fbaa54cec5cb7b2434

ENV PATH=/app/git-cinnabar:$PATH

RUN pip install --user requests

RUN git cinnabar download

# Install fzf for mach try fuzzy
WORKDIR /app/fzf

RUN git clone --depth 1 https://github.com/junegunn/fzf.git . \
    && ./install --bin

ENV PATH=/app/fzf/bin:$PATH

WORKDIR /app/wpt-sync

RUN git config --global user.name moz-wptsync-bot \
    && git config --global user.email wptsync@mozilla.com

# Copy test credentials as a temporary placeholder in workspace, and to testdata
RUN cp /app/wpt-sync/test/testdata/credentials.ini /app/workspace/credentials.ini \
    && cp /app/wpt-sync/test/testdata/sync.ini /app/workspace/sync.ini \
    && cp /app/wpt-sync/test/testdata/credentials.ini /app/testdata/credentials.ini \
    && cp /app/wpt-sync/test/testdata/sync.ini /app/testdata/sync.ini

RUN cp /app/wpt-sync/docker/known_hosts /app/.ssh/known_hosts

RUN mkdir -p /app/.ipython/profile_default/startup/
RUN cp 00-startup.py /app/.ipython/profile_default/startup/

# TODO make prod version that installs wheels?
# Install the app in development mode
RUN virtualenv /app/venv \
    && /app/venv/bin/pip install -r requirements/prod.txt --no-deps --require-hashes \
    && /app/venv/bin/pip install -r requirements/dev.txt --no-deps \
    && /app/venv/bin/pip install -r requirements/mozautomation.txt --no-deps

WORKDIR /app

# This puts the egg-info dir in /app, which won't be overwritten
# when docker mounts the volume for our source code
RUN /app/venv/bin/python /app/wpt-sync/setup.py develop --no-deps

ENV PYTHONPATH=/app/wpt-sync
ENV WPTSYNC_ROOT=/app/workspace
ENV WPTSYNC_REPO_ROOT=/app/repos
# Allow git-cinnabar to find latest hg
ENV PATH=/app/venv/bin:$PATH

# Mach requires that this is set
ENV SHELL=/bin/bash

WORKDIR /app/wpt-sync

# /app/wpt-sync: bind mount to src dir (only on dev) or dir with wheels?
# /app/workspace: bind mount to [empty] dir where service will write working data, logs
# config files, credentials, ssh config
# /app/repos: bind mount to ebs volume for gecko and wpt repos (or local dev repos)
VOLUME ["/app/wpt-sync", "/app/workspace", "/app/repos"]

ENTRYPOINT ["/app/start_wptsync.sh"]
CMD ["--worker"]
