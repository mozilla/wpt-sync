#!/usr/bin/env bash
# Run this from wptsync repo root
set -euo pipefail

# _image_name: name of docker image, tagged with relevant sha
#   e.g. wptsync_dev:cdc0d642c5ba17914e001e2a9b9fb6c893cfe57c
#   Since we're not rebuilding the image, provide name of most recent deployed image

# This should work if we've been conscientious about tagging builds with *-image
img="wptsync_dev:$(git rev-list -n 1 $(git tag --list .*-image | tail -1))"

if [ "$#" -ne 2 ]; then
    echo Please specify a suitable git tag and message for this commit.
    echo Usage: $0 \<tag\> \"\<message\>\"
    echo The last tag is $(git describe --abbrev=0)
    exit 1
fi

tag="${1-}"
msg="${2-}"

ANSIBLE_CONFIG="ansible/ansible.cfg" ansible-playbook -i ansible/hosts -f 20 \
    ansible/wptsync_update.yml -vvv \
    --extra-vars _image_name=$img

echo Creating tag $tag. Remember to push it.
git tag -a $tag -m "$msg"
