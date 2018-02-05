#!/usr/bin/env bash
# Run this from wptsync repo root

ANSIBLE_CONFIG="ansible/ansible.cfg" ansible-playbook -i ansible/hosts -f 20 \
    ansible/wptsync_deploy.yml -vvv \
    --extra-vars repo_root=$(pwd) \
    --extra-vars image_name=wptsync_dev \
    --extra-vars tempdir=$(pwd)/devenv/ansible_workspace
