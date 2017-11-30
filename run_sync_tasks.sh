#!/bin/bash

eval "$(ssh-agent -s)"
ssh-add ssh/id_github_ecdsa
celery worker --app sync.worker -B
