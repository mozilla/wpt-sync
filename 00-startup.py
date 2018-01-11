# Code so that we get a working environment on ipython startup

from sync import tasks

git_gecko, git_wpt = tasks.setup()
