import os
import shutil
import traceback
from datetime import datetime, timedelta

import git
import pygit2

import log
from base import ProcessName
from env import Environment
from lock import MutGuard, SyncLock, mut
from repos import pygit2_get, wrapper_get


env = Environment()

logger = log.get_logger(__name__)


def cleanup(git_gecko, git_wpt):
    for repo in [git_gecko, git_wpt]:
        pygit2_repo = pygit2_get(repo)
        cleanup_repo(pygit2_repo, get_max_worktree_count(repo))


def cleanup_repo(pygit2_repo, max_count=None):
    # TODO: Always cleanup repos where the sync is finished
    prune_worktrees(pygit2_repo)
    unprunable = []
    prunable = []
    now = datetime.now()
    for worktree in worktrees(pygit2_repo):
        if not os.path.exists(worktree.path):
            worktree.prune(True)
            continue

        pygit2_repo = pygit2.Repository(worktree.path)
        head_branch = pygit2_repo.head.name

        if not head_branch:
            logger.warning("No head associated with worktree %s" % worktree.path)
            unprunable.append(worktree)
            continue

        if head_branch.startswith("refs/heads/"):
            head_branch = head_branch[len("refs/heads/"):]

        process_name = ProcessName.from_path(head_branch)
        if process_name is None:
            logger.warning("No sync process associated with worktree %s" % worktree.path)
            unprunable.append(worktree)
            continue

        prunable.append((datetime.fromtimestamp(os.stat(worktree.path).st_mtime),
                         process_name,
                         worktree))

    if max_count and len(unprunable) > max_count:
        logger.error("Unable to cleanup worktrees, because there are too many unprunable worktrees")

    if not max_count:
        delete_count = 0
    else:
        delete_count = max(len(unprunable) + len(prunable) - max_count, 0)

    prunable.sort()
    for time, process_name, worktree in prunable:
        if time < (now - timedelta(days=2)):
            logger.info("Removing worktree without recent activity %s" % worktree.path)
            delete_worktree(process_name, worktree)
            delete_count -= 1
        elif delete_count > 0:
            logger.info("Removing LRU worktree %s" % worktree.path)
            delete_worktree(process_name, worktree)
            delete_count -= 1
        else:
            break


def delete_worktree(process_name, worktree):
    with SyncLock.for_process(process_name):
        try:
            shutil.rmtree(worktree.path)
        except Exception:
            logger.warning("Failed to remove worktree %s:%s" %
                           (worktree.path, traceback.format_exc()))
        else:
            logger.debug("Removed worktree %s" % (worktree.path,))
        worktree.prune(True)


def worktrees(pygit2_repo):
    for name in pygit2_repo.list_worktrees():
        yield pygit2_repo.lookup_worktree(name)


def prune_worktrees(pygit2_repo):
    for worktree in worktrees(pygit2_repo):
        # For some reason libgit2 thinks worktrees are not prunable when their
        # working dir is gone
        if worktree.is_prunable or not os.path.exists(worktree.path):
            logger.info("Deleting worktree at path %s" % worktree.path)
            worktree.prune(True)


def get_max_worktree_count(repo):
    repo_wrapper = wrapper_get(repo)
    if not repo_wrapper:
        return None
    repo_name = repo_wrapper.name
    max_count = env.config[repo_name]["worktree"]["max-count"]
    if not max_count:
        return None
    max_count = int(max_count)
    if max_count <= 0:
        return None
    return max_count


class Worktree(object):
    """Wrapper for accessing a git worktree for a specific process.

    To access the worktree call .get()
    """

    def __init__(self, repo, process_name):
        self.repo = repo
        self.pygit2_repo = pygit2_get(repo)
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
            count = len(list(worktrees(self.pygit2_repo)))
            max_count = get_max_worktree_count(self.repo)
            if max_count and count >= max_count:
                cleanup_repo(self.pygit2_repo, max_count - 1)
            if not os.path.exists(self.path):
                logger.info("Creating worktree %s" % self.path)
                prune_worktrees(self.pygit2_repo)
                self.pygit2_repo.add_worktree(self.process_name.obj_id,
                                              os.path.abspath(self.path),
                                              self.pygit2_repo.lookup_reference(
                                                  "refs/heads/%s" % self.process_name))
            self._worktree = git.Repo(self.path)
        # TODO: In general the worktree should be on the right branch, but it would
        # be good to check. In the specific case of landing, we move the wpt worktree
        # around various commits, so it isn't necessarily on the correct branch
        return self._worktree

    @mut()
    def delete(self):
        worktree = self.pygit2_repo.lookup_worktree(self.process_name.obj_id)
        if worktree is None:
            for worktree in worktrees(self.pygit2_repo):
                if worktree.path == self.path:
                    break
            else:
                # No worktree found
                return
        delete_worktree(self.process_name, self.pygit2_repo)
