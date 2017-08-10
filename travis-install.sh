#! /bin/bash
set -ex

SYNC_DIR=$PWD
pip install -r requirements.txt
pip install -e .
cd ~
git clone https://github.com/glandium/git-cinnabar.git cinnabar
cd cinnabar
git checkout 0.4.0
export PATH=$PWD:$PATH
git cinnabar download
cd $SYNC_DIR

set +ex
