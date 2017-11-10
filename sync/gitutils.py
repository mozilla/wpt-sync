import git
import log


logger = log.get_logger(__name__)


def update_repositories(git_gecko, git_wpt, repository_name=None):
    logger.info("Fetching mozilla-unified")
    # Not using the built in fetch() function since that tries to parse the output
    # and sometimes fails
    git_gecko.git.fetch("mozilla")
    logger.info("Fetch done")

    if repository_name == "autoland":
        logger.info("Fetch autoland")
        git_gecko.git.fetch("autoland")
        logger.info("Fetch done")

    git_wpt.git.fetch("origin")


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
    #TODO: Work out how to add these to the config when we set up the repo
    prefix = "refs/remotes/origin/pr/"
    git_wpt.remotes.origin.fetch("+refs/pull/*/head:%s*" % prefix)
    pr_refs = refs(git_wpt, prefix)
    if rev in pr_refs:
        return pr_refs[rev][len(prefix):]

