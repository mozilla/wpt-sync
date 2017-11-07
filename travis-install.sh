#! /bin/bash
set -ex

SYNC_DIR=$PWD
pip install -r prod-requirements.txt --no-deps
pip install -r requirements-mozautomation.txt --no-deps
pip install -e . --no-deps
cd ~
git clone https://github.com/glandium/git-cinnabar.git cinnabar
cd cinnabar
git checkout master
export PATH=$PWD:$PATH
git cinnabar download
cd $SYNC_DIR

set +ex
