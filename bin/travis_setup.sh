#!/usr/bin/env bash

set -euo pipefail

# This script must be sourced, so that the environment variables are set 
# in the calling shell.

# Use a clean virtualenv rather than the one given to us, to work around:
# https://github.com/travis-ci/travis-ci/issues/4873
if [[ ! -f "${HOME}/venv/bin/python" ]]; then
    echo '-----> Creating virtualenv'
    virtualenv -p python "${HOME}/venv"
fi
export PATH="${HOME}/venv/bin:${PATH}"

echo '-----> Running pip install'
pip install -r requirements/prod.txt --no-deps --require-hashes
pip check
pip install -r requirements/dev.txt --no-deps
pip install -r requirements/mozautomation.txt --no-deps

set +euo pipefail
