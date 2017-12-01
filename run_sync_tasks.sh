#!/bin/bash

eval "$(ssh-agent -s)"
ssh-add ssh/id_github_ecdsa
ssh-keyscan -H github.com >> ~/.ssh/known_hosts
celery worker --app sync.worker -B -c1
