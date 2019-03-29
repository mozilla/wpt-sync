import time

import git

import log
from env import Environment
from errors import RetryableError
from lock import RepoLock

env = Environment()


logger = log.get_logger(__name__)


def have_gecko_hg_commit(git_gecko, hg_rev):
    try:
        git_gecko.cinnabar.hg2git(hg_rev)
    except ValueError:
        return False
    return True


def update_repositories(git_gecko, git_wpt, include_autoland=False, wait_gecko_commit=None):
    if git_gecko is not None:
        if wait_gecko_commit is not None:
            success = until(lambda: _update_gecko(git_gecko, include_autoland),
                            lambda: have_gecko_hg_commit(git_gecko, wait_gecko_commit))
            if not success:
                raise RetryableError(
                    ValueError("Failed to fetch gecko commit %s" % wait_gecko_commit))
        else:
            _update_gecko(git_gecko, include_autoland)

    if git_wpt is not None:
        _update_wpt(git_wpt)


def until(func, cond, max_tries=5):
    for i in xrange(max_tries):
        func()
        if cond():
            break
        time.sleep(1 * (i + 1))
    else:
        return False
    return True


def _update_gecko(git_gecko, include_autoland):
    with RepoLock(git_gecko):
        logger.info("Fetching mozilla-unified")
        # Not using the built in fetch() function since that tries to parse the output
        # and sometimes fails
        git_gecko.git.fetch("mozilla")

        if include_autoland and "autoland" in [item.name for item in git_gecko.remotes]:
            logger.info("Fetching autoland")
            git_gecko.git.fetch("autoland")


def _update_wpt(git_wpt):
    with RepoLock(git_wpt):
        logger.info("Fetching web-platform-tests")
        git_wpt.git.fetch("origin")


def refs(git, prefix=None):
    rv = {}
    refs = git.git.show_ref().split("\n")
    for item in refs:
        sha1, ref = item.split(" ", 1)
        if prefix and not ref.startswith(prefix):
            continue
        rv[sha1] = ref
    return rv


def pr_for_commit(git_wpt, rev):
    prefix = "refs/remotes/origin/pr/"
    pr_refs = refs(git_wpt, prefix)
    if rev in pr_refs:
        return int(pr_refs[rev][len(prefix):])


def gecko_repo(git_gecko, head):
    repos = ([("central", env.config["gecko"]["refs"]["central"])] +
             [(name, ref) for name, ref in env.config["gecko"]["refs"].iteritems()
              if name != "central"])

    for name, ref in repos:
        if git_gecko.is_ancestor(head, ref):
            return name


def status(repo):
    status_entries = repo.git.status(z=True).split("\0")
    rv = {}
    for item in status_entries:
        if not item.strip():
            continue
        code = item[:2]
        filenames = item[3:].rsplit(" -> ", 1)
        if len(filenames) == 2:
            filename, rename = filenames
        else:
            filename, rename = filenames[0], None
        rv[filename] = {"code": code, "rename": rename}
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
    try:
        worktree.git.cherry_pick(commit)
        return True
    except git.GitCommandError as e:
        if handle_empty_commit(worktree, e):
            return True
        return False
