import git
import log
from env import Environment

env = Environment()


logger = log.get_logger(__name__)


def update_repositories(git_gecko, git_wpt, include_autoland=False):

    if git_gecko is not None:
        logger.info("Fetching mozilla-unified")
        # Not using the built in fetch() function since that tries to parse the output
        # and sometimes fails
        git_gecko.remotes.mozilla.fetch()

        if include_autoland and "autoland" in [item.name for item in git_gecko.remotes]:
            logger.info("Fetching autoland")
            git_gecko.remotes.autoland.fetch()

    if git_wpt is not None:
        logger.info("Fetching web-platform-tests")
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
        return int(pr_refs[rev][len(prefix):])


def gecko_repo(git_gecko, head):
    repos = ([("central", env.config["gecko"]["refs"]["central"])] +
             [(name, ref) for name, ref in env.config["gecko"]["refs"].iteritems()
              if name != "central"])

    for name, ref in repos:
        if git_gecko.is_ancestor(head, ref):
            return name
