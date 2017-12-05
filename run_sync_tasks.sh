#!/bin/bash

export SHELL=/bin/bash
eval "$(ssh-agent -s)"
ssh-add ssh/id_github_ecdsa
ssh-add ssh/id_mozilla_ecdsa
mkdir -p ~/.ssh
ssh-keyscan -H github.com >> ~/.ssh/known_hosts
ssh-keyscan -H hg.mozilla.org >> ~/.ssh/known_hosts
echo "Host hg.mozilla.org
User james@hoppipolla.co.uk
IdentityFile ~/wpt-sync/ssh/id_mozilla_ecdsa

Host github.com
IdentityFile ~/wpt-sync/ssh/id_github_ecdsa
" >> ~/.ssh/config
if [ "$1" == "--worker" ]; then
    celery worker --app sync.worker -B -c1 --loglevel DEBUG
fi
