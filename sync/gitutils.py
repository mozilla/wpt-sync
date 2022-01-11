from __future__ import absolute_import
import subprocess
import time

import git
from six import iteritems
from six.moves import range

from . import log
from .env import Environment
from .errors import RetryableError
from .lock import RepoLock

MYPY = False
if MYPY:
    from git.repo.base import Repo
    from typing import Any, Dict, Callable, Optional, Text

env = Environment()


logger = log.get_logger(__name__)


def have_gecko_hg_commit(git_gecko, hg_rev):
    # type: (Repo, Text) -> bool
    try:
        git_gecko.cinnabar.hg2git(hg_rev)
    except ValueError:
        return False
    return True


def update_repositories(git_gecko, git_wpt, wait_gecko_commit=None):
    # type: (Optional[Repo], Optional[Repo], Optional[Text]) -> None
    if git_gecko is not None:
        if wait_gecko_commit is not None:

            def wait_fn():
                assert wait_gecko_commit is not None
                return have_gecko_hg_commit(git_gecko, wait_gecko_commit)

            def _update():
                assert git_gecko is not None
                return _update_gecko(git_gecko)

            success = until(_update, wait_fn)
            if not success:
                raise RetryableError(
                    ValueError("Failed to fetch gecko commit %s" % wait_gecko_commit))
        else:
            _update_gecko(git_gecko)

    if git_wpt is not None:
        _update_wpt(git_wpt)


def until(func, cond, max_tries=5):
    # type: (Callable, Callable, int) -> bool
    for i in range(max_tries):
        func()
        if cond():
            break
        time.sleep(1 * (i + 1))
    else:
        return False
    return True


def _fetch(git_gecko, remote):
    cmd = ["git", "--git-dir", git_gecko.git_dir, "fetch", remote]
    logger.info(" ".join(cmd))
    subprocess.check_call(cmd)


def _update_gecko(git_gecko):
    # type: (Repo) -> None
    with RepoLock(git_gecko):
        logger.info("Fetching mozilla-unified")
        # Not using the built in fetch() function since that tries to parse the output
        # and sometimes fails
        _fetch(git_gecko, "mozilla")
        if "autoland" in [item.name for item in git_gecko.remotes]:
            logger.info("Fetching autoland")
            _fetch(git_gecko, "autoland")


def _update_wpt(git_wpt):
    # type: (Repo) -> None
    with RepoLock(git_wpt):
        logger.info("Fetching web-platform-tests")
        git_wpt.git.fetch("origin")


def refs(git, prefix=None):
    # type: (Repo, Optional[Text]) -> Dict[Text, Text]
    rv = {}
    refs = git.git.show_ref().split("\n")
    for item in refs:
        sha1, ref = item.split(" ", 1)
        if prefix and not ref.startswith(prefix):
            continue
        rv[sha1] = ref
    return rv


def pr_for_commit(git_wpt, rev):
    # type: (Repo, Text) -> Optional[int]
    prefix = "refs/remotes/origin/pr/"
    pr_refs = refs(git_wpt, prefix)
    if rev in pr_refs:
        return int(pr_refs[rev][len(prefix):])
    return None


def gecko_repo(git_gecko, head):
    # type: (Repo, Commit) -> Optional[Text]
    repos = ([("central", env.config["gecko"]["refs"]["central"])] +
             [(name, ref) for name, ref in iteritems(env.config["gecko"]["refs"])
              if name != "central"])

    for name, ref in repos:
        if git_gecko.is_ancestor(head, ref):
            return name
    return None


def status(repo):
    # type: (Repo) -> Dict[Text, Dict[Text, Any]]
    status_entries = repo.git.status(z=True).split("\0")
    rv = {}
    for item in status_entries:
        if not item.strip():
            continue
        code = item[:2]
        filenames = item[3:].rsplit(u" -> ", 1)
        if len(filenames) == 2:
            filename, rename = filenames
        else:
            filename, rename = filenames[0], None
        rv[filename] = {u"code": code, u"rename": rename}
    return rv


def handle_empty_commit(worktree, e):
    # If git exits with return code 1 and mentions an empty
    # cherry pick, then we tried to cherry pick something
    # that results in an empty commit so reset the index and
    # continue. gitpython doesn't really enforce anything about
    # the type of status, so just convert it to a string to be
    # sure
    if (str(e.status) == "1" and
        "The previous cherry-pick is now empty" in e.stderr or
        "nothing to commit" in e.stdout):
        logger.info("Cherry pick resulted in an empty commit")
        # If the cherry pick would result in an empty commit,
        # just reset and continue
        worktree.git.reset()
        return True
    return False


def cherry_pick(worktree, commit):
    # type: (Repo, Text) -> bool
    try:
        worktree.git.cherry_pick(commit)
        return True
    except git.GitCommandError as e:
        if handle_empty_commit(worktree, e):
            return True
        return False
