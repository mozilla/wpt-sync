#!/usr/bin/env bash
# Run this from wptsync repo root
set -euo pipefail

# WPT_SSH_HGMO: path to private key; assuming pk is $WPT_SSH_HGMO.pub
# WPT_SSH_GITHUB: path to private key; assuming pk is $WPT_SSH_GITHUB.pub
# WPT_CREDENTIALS: path to production credentials ini

# _repo_root: path to wpt-sync repo
# _image_name: name of docker image, tagged with HEAD sha
# _tempdir: path to workspace for ansible

img="wptsync_dev:$(git rev-parse HEAD)"

ANSIBLE_CONFIG="ansible/ansible.cfg" ansible-playbook -i ansible/hosts -f 20 \
    ansible/wptsync_deploy.yml -vvv \
    --extra-vars _repo_root=$(pwd) \
    --extra-vars _image_name=$img \
    --extra-vars _tempdir=$(pwd)/devenv/ansible_workspace \
    --extra-vars _ssh_hgmo=$WPT_SSH_HGMO \
    --extra-vars _ssh_github=$WPT_SSH_GITHUB \
    --extra-vars _credentials=$WPT_CREDENTIALS
