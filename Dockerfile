# Use an official Python runtime as a parent image
FROM ubuntu

RUN apt-get update && \
    apt-get install -y libcurl3 git python python-pip python-requests emacs24-nox

RUN useradd -ms /bin/bash wptsync

USER wptsync

RUN mkdir /home/wptsync/git-cinnabar

WORKDIR /home/wptsync/git-cinnabar

RUN git clone https://github.com/glandium/git-cinnabar.git . && git checkout origin/next

ENV PATH=/home/wptsync/git-cinnabar:$PATH

RUN git cinnabar download

RUN mkdir /home/wptsync/wpt-sync

WORKDIR /home/wptsync/wpt-sync

RUN git config --global user.name wpt-sync && \
    git config --global user.email wpt-sync@lists.mozilla.com

# Copy the current directory contents into the container at /app
ADD . /home/wptsync/wpt-sync

RUN pip install -r prod-requirements.txt --no-deps
RUN pip install -r requirements-dev.txt --no-deps
RUN pip install -r requirements-mozautomation.txt --no-deps
RUN pip install -e . --no-deps
# TODO: make this part of a setup script
# RUN python sync/repos.py
# RUN python sync/model.py

ENV PATH=/home/wptsync/.local/bin:$PATH
