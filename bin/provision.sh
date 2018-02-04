#!/usr/bin/env bash

ANSIBLE_CONFIG="ansible/ansible.cfg" ansible-playbook -i ansible/hosts -f 20 ansible/wptsync_deploy.yml -vvv
