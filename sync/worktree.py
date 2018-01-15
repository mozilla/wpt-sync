import os
import shutil
from datetime import datetime, timedelta

import log
from base import ProcessName

logger = log.get_logger(__name__)


def cleanup(git_gecko, git_wpt):
    for git in [git_gecko, git_wpt]:
        git.git.worktree("prune")
        worktrees = git.git.worktree("list", "--porcelain")
        groups = [item for item in worktrees.split("\n\n") if item.strip()]
        for group in groups:
            data = {}
            lines = group.split("\n")
            for line in lines:
                if " " in line:
                    key, value = line.split(" ", 1)
                else:
                    key, value = line, None
                data[key] = value

            if "bare" in data or "branch" not in data:
                continue

            logger.info("Checking worktree %s for branch %s" % (data["worktree"], data["branch"]))
            worktree_path = data["worktree"]

            process_name = ProcessName.from_ref(data["branch"])
            if process_name is None:
                continue

            if process_name.status != "open":
                logger.info("Removing worktree for closed sync %s" % worktree_path)
                shutil.rmtree(worktree_path)
                continue

            now = datetime.now()
            # Data hasn't been touched in two days
            if (datetime.fromtimestamp(os.stat(worktree_path).st_mtime) <
                now - timedelta(days=2)):
                logger.info("Removing worktree without recent activity %s" % worktree_path)
                shutil.rmtree(worktree_path)
