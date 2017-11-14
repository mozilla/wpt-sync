import git
import log


logger = log.get_logger(__name__)


def update_repositories(git_gecko, git_wpt, include_autoland=False):
    logger.info("Fetching mozilla-unified")
    # Not using the built in fetch() function since that tries to parse the output
    # and sometimes fails
    git_gecko.remotes.mozilla.fetch()
    logger.info("Fetch done")

    if include_autoland and "autoland" in [item.name for item in git_gecko.remotes]:
        logger.info("Fetch autoland")
        git_gecko.remotes.autoland.fetch*()
        logger.info("Fetch done")

    git_wpt.remotes.origin.fetch()


def is_ancestor(git_obj, rev, branch):
    try:
        git_obj.git.merge_base(rev, branch, is_ancestor=True)
    except git.GitCommandError:
        return False
    return True


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
        return pr_refs[rev][len(prefix):]
