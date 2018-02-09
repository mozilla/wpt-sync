#!/usr/bin/env bash
# Run this from wptsync repo root
set -euo pipefail

ANSIBLE_CONFIG="ansible/ansible.cfg" ansible-playbook -i ansible/hosts -f 20 \
    ansible/wptsync_update.yml -vvv 
