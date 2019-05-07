import os
import shutil
import traceback
from datetime import datetime, timedelta

import log
from base import ProcessName
from env import Environment
from lock import MutGuard, SyncLock, mut


env = Environment()

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

            if not os.path.exists(worktree_path):
                continue

            with SyncLock.for_process(process_name):
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
        git.git.worktree("prune")


class Worktree(object):
    """Wrapper for accessing a git worktree for a specific process.

    To access the worktree call .get()
    """

    def __init__(self, repo, process_name):
        self.repo = repo
        self._worktree = None
        self.process_name = process_name
        self.path = os.path.join(env.config["root"],
                                 env.config["paths"]["worktrees"],
                                 os.path.basename(repo.working_dir),
                                 process_name.subtype,
                                 process_name.obj_id)
        self._lock = None

    def as_mut(self, lock):
        return MutGuard(lock, self)

    @property
    def lock_key(self):
        return (self.process_name.subtype, self.process_name.obj_id)

    @mut()
    def get(self):
        """Return the worktree.

        On first access, the worktree is reset to the current HEAD. Subsequent
        access doesn't perform the same check, so it's possible to retain state
        within a specific process."""
        # TODO: We can get the worktree to only checkout the paths we actually
        # need.
        # To do this we have to
        # * Enable sparse checkouts by setting core.sparseCheckouts
        # * Add the worktree with --no-checkout
        # * Add the list of paths to check out under $REPO/worktrees/info/sparse-checkout
        # * Go to the worktree and check it out
        if self._worktree is None:
            if not os.path.exists(self.path):
                self.repo.git.worktree("prune")
                self.repo.git.worktree("add",
                                       os.path.abspath(self.path),
                                       str(self.process_name))
            self._worktree = git.Repo(self.path)
        # TODO: In general the worktree should be on the right branch, but it would
        # be good to check. In the specific case of landing, we move the wpt worktree
        # around various commits, so it isn't necessarily on the correct branch
        return self._worktree

    @mut()
    def delete(self):
        if os.path.exists(self.path):
            logger.info("Deleting worktree at %s" % self.path)
            try:
                shutil.rmtree(self.path)
            except Exception:
                logger.warning("Failed to remove worktree %s:%s" %
                               (self.path, traceback.format_exc()))
            else:
                logger.debug("Removed worktree %s" % (self.path,))
        self.repo.git.worktree("prune")
