#!/usr/bin/env bash
# Run this from wptsync repo root
set -euo pipefail

# The "install credentials" step will be skipped unless all three of these
# environment variables are set.
#   WPT_SSH_HGMO: path to private key; assuming pk is $WPT_SSH_HGMO.pub
#   WPT_SSH_GITHUB: path to private key; assuming pk is $WPT_SSH_GITHUB.pub
#   WPT_CREDENTIALS: path to production credentials ini

# _repo_root: path to wpt-sync repo
# _image_name: name of docker image, tagged with HEAD sha
# _tempdir: path to workspace for ansible

if [ "$#" -ne 2 ]; then
    echo Please specify a suitable git tag and message for this commit.
    echo Usage: $0 \<tag\> \"\<message\>\"
    echo The last tag associated with a docker build is $(git tag --list .*-image | tail -1)
    exit 1
fi

if [ ! -f $(pwd)/config/prod/sync.ini ]; then
    echo Please add a sync.ini file to $(PWD)/config/prod
fi

if [ -z ${WPT_CREDENTIALS} ]; then
    if [ ! -f $(pwd)/config/prod/credentials.ini ]; then
        echo Please add a credentials.ini file to $(PWD)/config/prod
    fi
fi

img="wptsync_dev:$(git rev-parse HEAD)"
tag="${1-}"
msg="${2-}"

ANSIBLE_CONFIG="ansible/ansible.cfg" ansible-playbook -i ansible/hosts -f 20 \
    ansible/wptsync_deploy.yml -vvv \
    --extra-vars _repo_root=$(pwd) \
    --extra-vars _image_name=$img \
    --extra-vars _tempdir=$(pwd)/ansible_workspace \

echo Creating tag $tag. Remember to push it.
git tag -a $tag -m "$msg"
