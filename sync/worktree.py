import os
import shutil
import tempfile
import traceback
from datetime import datetime, timedelta

import git
import pygit2

from . import log
from .base import ProcessName
from .env import Environment
from .lock import MutGuard, SyncLock, mut
from .repos import pygit2_get, wrapper_get

from git.repo.base import Repo
from pygit2 import Worktree as PyGit2Worktree
from pygit2.repository import Repository
from typing import Any, Iterator, Optional, Tuple

env = Environment()

logger = log.get_logger(__name__)


def cleanup(git_gecko: Repo, git_wpt: Repo) -> None:
    for repo in [git_gecko, git_wpt]:
        pygit2_repo = pygit2_get(repo)
        cleanup_repo(repo, pygit2_repo, get_max_worktree_count(repo))


def cleanup_repo(repo: Repo, pygit2_repo: Repository, max_count: Optional[int] = None) -> None:
    # TODO: Always cleanup repos where the sync is finished
    prune_worktrees(pygit2_repo)
    unprunable = []
    maybe_prunable = []
    prunable = []
    now = datetime.now()
    for worktree in worktrees(pygit2_repo):
        if not os.path.exists(worktree.path):
            prune_worktree(worktree)
            continue

        process_name = ProcessName.from_tuple(worktree.name.split("-"))

        worktree_data = (
            datetime.fromtimestamp(os.stat(worktree.path).st_mtime),
            process_name,
            worktree,
        )

        if process_name is None:
            logger.warning("Worktree doesn't correspond to a sync %s" % worktree.path)
            unprunable.append(worktree_data)
            continue

        worktree_pygit2_repo = get_worktree_repo(worktree.path)
        if worktree_pygit2_repo is None:
            # Removed concurrently
            prune_worktree(worktree)
            continue
        head_branch = worktree_pygit2_repo.head.name if worktree_pygit2_repo else None

        if not head_branch or head_branch == "HEAD":
            logger.warning("No branch associated with worktree %s" % worktree.path)
            maybe_prunable.append(worktree_data)
            continue

        if head_branch.startswith("refs/heads/"):
            head_branch = head_branch[len("refs/heads/") :]

        branch_process_name = ProcessName.from_path(head_branch)
        if branch_process_name is None:
            logger.warning("No sync head associated with worktree %s" % worktree.path)
            maybe_prunable.append(worktree_data)
            continue

        if branch_process_name != process_name:
            logger.warning("Head branch doesn't match worktree %s" % worktree.path)
            maybe_prunable.append(worktree_data)
            continue

        prunable.append(worktree_data)

    if max_count and len(unprunable) > max_count:
        logger.error("Unable to cleanup worktrees, because there are too many unprunable worktrees")

    if not max_count:
        delete_count = 0
    else:
        delete_count = max(len(unprunable) + len(prunable) + len(maybe_prunable) - max_count, 0)

    prunable.sort()
    maybe_prunable.sort()
    for time, process_name, worktree in prunable + maybe_prunable:
        assert process_name is not None
        if time < (now - timedelta(days=2)):
            logger.info("Removing worktree without recent activity %s" % worktree.path)
            delete_worktree(repo, process_name, worktree)
            delete_count -= 1
        elif delete_count > 0:
            logger.info("Removing LRU worktree %s" % worktree.path)
            delete_worktree(repo, process_name, worktree)
            delete_count -= 1
        else:
            break


def delete_worktree(repo: Repo, process_name: ProcessName, worktree: PyGit2Worktree) -> None:
    assert worktree.path.startswith(
        os.path.join(env.config["root"], env.config["paths"]["worktrees"])
    )
    with SyncLock.for_process(process_name):
        prune_worktree(worktree)

        wrapper = wrapper_get(repo)
        assert wrapper is not None
        wrapper.after_worktree_delete(worktree.path)


def delete_path(path: str) -> None:
    try:
        logger.debug(f"Deleting path %{path}")
        shutil.rmtree(path)
    except Exception:
        if os.path.exists(path):
            logger.warning(f"Failed to remove {path}:{traceback.format_exc()}")


def get_worktree_repo(path: str) -> Optional[Repository]:
    try:
        return pygit2.Repository(path)
    except pygit2.GitError:
        return None


def worktrees(pygit2_repo: Repository) -> Iterator[PyGit2Worktree]:
    for name in pygit2_repo.list_worktrees():
        try:
            yield pygit2_repo.lookup_worktree(name)
        except pygit2.GitError:
            # The worktree may have been pruned by another process since it was listed
            logger.warning(f"Unable to look up worktree {name}, skipping")


def prune_worktree(worktree: PyGit2Worktree) -> None:
    try:
        worktree.prune(True)
    except pygit2.GitError:
        # Most likely the worktree was already pruned by another process
        if os.path.exists(worktree.path):
            logger.warning(f"Failed to prune worktree {worktree.path}: {traceback.format_exc()}")

    delete_path(worktree.path)


def prune_worktrees(pygit2_repo: Repository) -> None:
    for worktree in worktrees(pygit2_repo):
        # For some reason libgit2 thinks worktrees are not prunable when their
        # working dir is gone
        if worktree.is_prunable or not os.path.exists(worktree.path):
            logger.info("Deleting worktree at path %s" % worktree.path)
            prune_worktree(worktree)


def get_max_worktree_count(repo: Repo) -> Optional[Any]:
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


def get_sparse_checkout(repo: Repo) -> Optional[str]:
    """Path to the file containing the sparse checkout patterns for the worktrees
    of a repo, or None if the worktrees should contain all the files in the repo."""
    repo_wrapper = wrapper_get(repo)
    # The patterns are for the paths in gecko, so they mustn't be applied to the
    # other repos, where they would leave out almost all the files
    if not repo_wrapper or repo_wrapper.name != "gecko":
        return None
    path = "/app/config/try-wpt.sparse"
    if not path:
        return None
    return path


def read_sparse_checkout(path: str) -> Optional[bytes]:
    """Sparse checkout patterns in the file at a path, or None if it can't be read."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        logger.error(
            f"Failed to read sparse checkout patterns from {path}, "
            f"checking out all the files instead:\n{traceback.format_exc()}"
        )
        return None


def enable_sparse_checkout(repo: Repo) -> None:
    """Ensure that git applies the sparse checkout patterns in the worktrees of a repo."""

    try:
        enabled = repo.git.config("--type=bool", "--get", "core.sparseCheckout") == "true"
    except git.GitCommandError:
        # The setting isn't in the config at all
        enabled = False
    if not enabled:
        logger.info("Enabling sparse checkouts in %s" % repo.git_dir)
        repo.git.config("core.sparseCheckout", "true")


class Worktree:
    """Wrapper for accessing a git worktree for a specific process.

    To access the worktree call .get()
    """

    def __init__(self, repo: Repo, process_name: ProcessName) -> None:
        self.repo = repo
        self.pygit2_repo = pygit2_get(repo)
        self._worktree: Optional[Repo] = None
        self.process_name = process_name
        self.worktree_name = "-".join(str(item) for item in self.process_name.as_tuple())
        working_dir = repo.working_dir
        assert working_dir is not None
        self.path = os.path.join(
            env.config["root"],
            env.config["paths"]["worktrees"],
            os.path.basename(working_dir),
            process_name.subtype,
            process_name.obj_id,
        )
        self._lock = None

    def as_mut(self, lock: SyncLock) -> MutGuard:
        return MutGuard(lock, self)

    @property
    def lock_key(self) -> Tuple[str, str]:
        return (self.process_name.subtype, self.process_name.obj_id)

    @mut()
    def get(self) -> Repo:
        """Return the worktree.

        On first access, the worktree is reset to the current HEAD. Subsequent
        access doesn't perform the same check, so it's possible to retain state
        within a specific process."""
        if self._worktree is None:
            sparse_checkout = get_sparse_checkout(self.repo)
            if sparse_checkout is not None:
                # This is needed for the existing worktrees too, and the repo config
                # is replaced whenever it's installed again with repo-config
                enable_sparse_checkout(self.repo)

            all_worktrees = {item.name: item for item in worktrees(self.pygit2_repo)}
            count = len(all_worktrees)
            max_count = get_max_worktree_count(self.repo)
            if max_count and count >= max_count:
                cleanup_repo(self.repo, self.pygit2_repo, max_count - 1)

            path_exists = os.path.exists(self.path)

            if self.worktree_name in all_worktrees:
                if path_exists:
                    if get_worktree_repo(self.path) is None:
                        logger.warning(
                            f"Worktree {self.path} is not a valid repository, recreating"
                        )
                        delete_path(self.path)
                        path_exists = False
                else:
                    prune_worktrees(self.pygit2_repo)
                    del all_worktrees[self.worktree_name]

            if self.worktree_name not in all_worktrees:
                if path_exists:
                    logger.warning(
                        "Found existing content in worktree path %s, removing" % self.path
                    )
                    delete_path(self.path)
                logger.info(f"Creating worktree {self.worktree_name} at {self.path}")
                if not os.path.exists(os.path.dirname(self.path)):
                    os.makedirs(os.path.dirname(self.path))
                patterns = None
                if sparse_checkout is not None:
                    patterns = read_sparse_checkout(sparse_checkout)
                if patterns is not None:
                    worktree = self.add_sparse(patterns)
                else:
                    worktree = self.pygit2_repo.add_worktree(
                        self.worktree_name,
                        os.path.abspath(self.path),
                        self.pygit2_repo.lookup_reference("refs/heads/%s" % self.process_name),
                    )
                wrapper = wrapper_get(self.repo)
                assert wrapper is not None
                wrapper.after_worktree_create(self.path)
            else:
                worktree = self.pygit2_repo.lookup_worktree(self.worktree_name)

            assert os.path.exists(self.path)
            assert worktree.path == self.path
            self._worktree = git.Repo(self.path)

        # TODO: In general the worktree should be on the right branch, but it would
        # be good to check. In the specific case of landing, we move the wpt worktree
        # around various commits, so it isn't necessarily on the correct branch
        assert self._worktree is not None
        return self._worktree

    def add_sparse(self, patterns: bytes) -> PyGit2Worktree:
        """Create the worktree with only the files matching the sparse checkout
        patterns checked out."""
        # A previous attempt to create the worktree could have been killed part way
        self.remove_incomplete()

        try:
            # libgit2 can't do sparse checkouts, so this has to use the git cli. That
            # always names a worktree after the last component of its path, so
            # create the worktree in a temporary directory to get the name we use,
            # and move it to the right path before any files are checked out.
            tmp_dir = tempfile.mkdtemp(prefix="tmp-", dir=os.path.dirname(self.path))
            try:
                tmp_path = os.path.join(tmp_dir, self.worktree_name)
                self.repo.git.worktree("add", "--no-checkout", tmp_path, str(self.process_name))
                self.repo.git.worktree("move", tmp_path, self.path)
            finally:
                delete_path(tmp_dir)

            git_work = git.Repo(self.path)

            with tempfile.TemporaryFile() as f:
                f.write(patterns)
                f.seek(0)
                git_work.git.sparse_checkout("set", "--stdin", istream=f)

            git_work.git.reset(hard=True)
        except Exception:
            # Otherwise the next access would use a worktree that's missing files
            self.remove_incomplete()
            raise

        return self.pygit2_repo.lookup_worktree(self.worktree_name)

    def remove_incomplete(self) -> None:
        """Remove anything left by an attempt to create the worktree that didn't finish."""
        try:
            worktree = self.pygit2_repo.lookup_worktree(self.worktree_name)
        except (KeyError, pygit2.GitError):
            # There's no worktree with this name; which of these we get depends
            # on the version of pygit2
            pass
        else:
            prune_worktree(worktree)
        if os.path.exists(self.path):
            delete_path(self.path)

    @mut()
    def recreate(self) -> Repo:
        """Delete the worktree and create it again from the current HEAD of its
        branch, e.g. to apply a change to the sparse checkout patterns.

        Any changes in the worktree that weren't committed are lost."""
        self.delete()
        return self.get()

    @mut()
    def delete(self) -> None:
        self._worktree = None
        if not os.path.exists(self.path):
            return
        try:
            worktree = self.pygit2_repo.lookup_worktree(self.worktree_name)
        except Exception:
            worktree = None
        if worktree is None:
            for worktree in worktrees(self.pygit2_repo):
                if worktree.path == self.path:
                    break
            else:
                # No worktree found
                return
        assert worktree.path == self.path
        delete_worktree(self.repo, self.process_name, worktree)
