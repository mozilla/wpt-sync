import os
import re
import subprocess
import sys
import time
import uuid
from collections import defaultdict

import git
import gh
from mozautomation import commitparser
from sqlalchemy.orm import joinedload

import log
import model
import repos
import settings
from model import Sync, Commit, Repository

logger = log.get_logger("update")


def partition(iterator, f):
    rv = [], []
    for item in iterator:
        idx = 0 if f(item) else 1
        rv[idx].append(item)
    yield rv


def get_backouts(repo, commits):
    # Handle closing PRs with no associated commits left later

    backout_revs = set()

    commit_revs = {item.hexsha for item in commits}
    # Mapping from backout rev to list of commits it backs out
    backout_commits = {}

    for commit in commits:
        if commitparser.is_backout(commit.message):
            nodes_bugs = commitparser.parse_backouts(commit.message)
            if nodes_bugs is None:
                # We think this a backout, but have no idea what it backs out
                # it's not clear how to handle that case
                # TODO logging here
                raise NotImplemetedError

            nodes, bugs = nodes_bugs
            # Assuming that all commits are listed.

            backout_commits[commit.hexsha] = set(repo.cinnabar.hg2git(node) for node in nodes)


    def backout_creates_noop(rev):
        """Determine whether all the commits backed out are other commits in
        this set i.e. the net result of applying the backout to the earlier commits
        is a noop"""
        backs_out_revs = backout_commits.get(rev)
        if not backs_out_revs:
            return False
        return all(rev in commit_revs for rev in backs_out_revs)


    # Filter out any backouts that touch things not in the current set of commits
    complete_backouts = {rev: value for rev, value in backout_commits.iteritems()
                         if backout_creates_noop(rev)}

    # Commits that have been backed out by a complete backout
    backed_out = set()
    for value in complete_backouts.itervalues():
        backed_out |= value

    # Filter commits to just leave ones that remain after a backout
    remaining_commits = [item for item in commits
                         if item.hexsha not in complete_backouts and
                         item.hexsha not in backed_out]


    return complete_backouts, remaining_commits


def is_empty(commit, path):
    data = commit.repo.git.show(commit.hexsha, "--", path,
                                patch=True,binary=True, format="format:").strip()
    return data == ""


def update_for_backouts(session, git_gecko, backout_revs):
    hg_revs = set()
    for backed_out in backout_revs.itervalues():
        hg_revs |= set(git_gecko.cinnabar.git2hg(item) for item in backed_out)

    syncs = session.query(Sync).join(Commit).filter(Commit.rev.in_(hg_revs))

    # Remove the commits from the database
    for sync in syncs:
        for commit in sync.commits:
            if commit.rev in nodes:
                session.delete(commit)

    return syncs


def abort_sync(session, gh_wpt, sync):
    gh_wpt.close_pull(sync.pr)
    sync.closed = True


def group_by_bug(commits):
    rv = defaultdict(list)
    for commit in commits:
        bugs = commitparser.parse_bugs(commit.message)
        #TODO: log is we get > 1 bug
        bug = bugs[0]
        rv[bug].append(commit)
    return rv


def get_unique_name(bug):
    pass


def update_sync_commits(session, git_gecko, repo_name, commits_by_bug):
    """Update the list of commits for each sync, based on those found in the latest set of
    changes, and return a list of syncs that need to be updated on the wpt side"""

    updated = []

    for bug, commits in commits_by_bug.iteritems():
        #TODO: log this and continue or something
        assert commits

        sync = (session.query(Sync)
                .options(joinedload(Sync.commits))
                .filter(Sync.bug==bug, Sync.merged==False)).first()

        if sync is None:
            sync = Sync(bug=bug, repository=Repository.by_name(session, repo_name))
            session.add(sync)

        if not sync.commits:
            # This is either new or backed out and relanded,
            # so create a new set of commits
            for commit in commits:
                commit = Commit(rev=git_gecko.cinnabar.git2hg(commit.hexsha))
                sync.commits.append(commit)
                session.add(commit)
        else:
            new_commits = [git_gecko.git2hg(item.hexsha) for item in commits]
            prev_commits = [item.rev for item in sync.commits]
            if not new_commits[:len(prev_commits)] == prev_commits:
                # The commits changed, so we want to do something sensible
                # There are a couple of possibilities:
                #  * The commits for the bug changed because of a history
                #    rewrite on autoland. In this case we can just replace the old commits
                #    with the new ones and keep going
                #  * The old commits made it to mozilla-central and so aren't in the
                #    loaded set, so we want to append to the existing PR

                #TODO : check if the commits still exist using
                # git merge-base --is-ancecstor upstream/repo_name rev

                prev_revs = set(prev_commits)
                for rev in new_commits:
                    if rev not in prev_revs:
                        sync.commits.append(Commit(rev=rev))
            else:
                # More commits were added to a bug that already has some
                for rev in new_commits[len(prev_commits):]:
                    sync.commits.append(Commit(rev=rev))

        updated.append(sync)

        return updated


def update_syncs(config, session, git_gecko, git_wpt, gh_wpt, syncs):
    git_wpt.remotes.origin.fetch()
    # Ensure that any worktrees that got deleted are not in the git config
    git_wpt.git.worktree("prune")

    for sync in syncs:
        if not sync.commits:
            abort_sync(session, gh_wpt, sync)
            continue

        if not sync.wpt_worktree:
            path = get_worktree_path(config, session, git_wpt, "web-platform-tests", str(sync.bug))
            sync.wpt_worktree = path
            logger.info("Setting up worktree in path %s" % path)

        worktree_path = os.path.join(config["paths"]["worktrees"], sync.wpt_worktree)

        # TODO: If we want to prune these need to be careful about atomicity here
        # Probably need to have a lock whilst the cleanup is running
        if not os.path.exists(worktree_path):
            base_dir, branch_name = os.path.split(worktree_path)
            try:
                os.makedirs(base_dir)
            except OSError:
                pass
            git_wpt.git.worktree("add", "-b", branch_name, os.path.abspath(worktree_path), "origin/master")
        else:
            branch_name = os.path.split(worktree_path)[1]

        git_work = git.Repo(worktree_path)
        git_work.index.reset("origin/master", hard=True)

        # TODO: what about merge commits?
        for commit in sync.commits:
            rev = git_gecko.cinnabar.hg2git(commit.rev)
            # git show might work better here
            # In particular this probably doesn't work with merge commits
            patch = git_gecko.git.format_patch("%s^..%s" % (rev, rev), "--", config["gecko"]["path"]["wpt"],
                                               stdout=True, patch=True)

            strip_dirs = len(config["gecko"]["path"]["wpt"].split("/")) + 1
            proc = git_work.git.am("-", "-p", str(strip_dirs),istream=subprocess.PIPE, as_process=True)
            stdout, stderr = proc.communicate(patch)
            if proc.returncode != 0:
                raise git.cmd.GitCommandError(["am", "-", "-p", str(strip_dirs)],
                                              proc.returncode, stderr, stdout)

            commit = git_work.head.commit
            msg = commit.message

            m = commitparser.BUG_RE.match(msg)
            if m:
                bug_str = m.group(0)
                if msg.startswith(bug_str):
                    # Strip the bug prefix
                    prefix = re.compile("^%s[^\w\d]*" % bug_str)
                    msg = prefix.sub("", msg)

            msg = commitparser.strip_commit_metadata(msg)

            git_work.git.commit(amend=True, message=msg)

        if not sync.remote_branch:
            sync.remote_branch = "gecko_%s_%s" % (branch_name, uuid.uuid4())
        # TODO: maybe if there's >1 commit use the bug title in the PR title

        git_wpt.remotes["origin"].push("%s:%s" % (branch_name, sync.remote_branch), force=True)

        bugzilla_url = "https://bugzilla.mozilla.org/show_bug.cgi?id=%s" % sync.bug if sync.bug else None

        if not sync.pr:
            while not gh_wpt.get_branch(sync.remote_branch):
                logger.debug("Waiting for branch")
                time.sleep(1)

            pr_body = "Upstreamed from " + bugzilla_url if bugzilla_url else "Upstreamed from gecko"
            pr = gh_wpt.create_pull(title="[Gecko] %s" % msg.splitlines()[0],
                                    body=pr_body,
                                    base="master",
                                    head=sync.remote_branch)
            sync.pr = pr.id

        gh_wpt.set_status(sync.pr, "failure", target_url=bugzilla_url, description="Landed on mozilla-central",
        context="upstream/gecko")


def get_worktree_path(config, session, git_wpt, subdir, prefix):
    base_path = os.path.join(subdir, prefix)
    count = 0
    while True:
        rel_path = base_path
        if count > 0:
            rel_path = "%s-%i" % (rel_path, count)
        path = os.path.join(config["paths"]["worktrees"], rel_path)
        branch_name = os.path.split(rel_path)[1]
        if not (os.path.exists(path) or
                branch_name in git_wpt.branches or
                session.query(Sync).filter(Sync.wpt_worktree == rel_path).first()):
            return rel_path
        count += 1


def do_update(config, repo_name, session, git_gecko, git_wpt, gh_wpt):
    try:
        repository, _ = model.get_or_create(session, model.Repository, name=repo_name)

        logger.info("Fetching mozilla-unified")
        # Not using the built in fetch() function since that tries to parse the output
        # and sometimes fails
        git_gecko.git.fetch("mozilla")
        logger.info("Fetch done")
        if repo_name == "autoland":
            logger.info("Fetch autoland")
            git_gecko.git.fetch("autoland")
            logger.info("Fetch done")

        gecko_wpt_path = config["gecko"]["path"]["wpt"]

        landing = config["sync"]["landing"].rsplit("/", 1)[1]
        # TODO: simplify this a little
        revish = "%s..%s" % (config["gecko"]["refs"]["central"],
                             config["gecko"]["refs"][repo_name])

        all_commits = list(git_gecko.iter_commits(revish))
        if all_commits:
            logger.debug("Found %i new commits from central starting at %s" % (len(all_commits), all_commits[0].hexsha))
        else:
            logger.debug("No new commits since central")
        wpt_commits = [item.hexsha for item in
                       git_gecko.iter_commits(revish, paths=gecko_wpt_path)]

        if not wpt_commits:
            logger.info("No commits updated %s" % gecko_wpt_path)
            return

        logger.info("Found %i commits affecting wpt: %s" % (len(wpt_commits), " ".join(wpt_commits)))
        wpt_commits = set(wpt_commits)

        # First remove backouts. This must be done across all commits
        # so that we can tell if a backout only touches things in the set
        # we are considering
        backout_revs, no_backout_commits = get_backouts(git_gecko, all_commits)

        #Now filter down to just wpt commits with backouts removed
        commits = [item for item in no_backout_commits if item.hexsha in wpt_commits]

        if commits:
            logger.info("%i commits remain after removing backouts" % (len(wpt_commits),))
        else:
            logger.info("No commits remaining after removing backouts")

        #Now remove empty commits (probably merges)
        commits = [item for item in commits if not is_empty(item, gecko_wpt_path)]

        if commits:
            logger.info("%i commits remain after removing empty commits" % (len(wpt_commits),))
        else:
            logger.info("No commits remaining after removing empty commits")

        # List of syncs that have changed, so we can update them all as appropriate at the end
        modified_syncs = []
        modified_syncs.extend(update_for_backouts(session, git_gecko, backout_revs))

        if commits:
            by_bug = group_by_bug(commits)

            # There is one worrying case we don't handle yet. What if a bug lands in central,
            # but before we mark the commits as on-central, we get a new commit for the same bug
            # In that case we end up overwriting the old commits.
            modified_syncs.extend(update_sync_commits(session, git_gecko, repo_name, by_bug))

        if modified_syncs:
            update_syncs(config, session, git_gecko, git_wpt, gh_wpt, modified_syncs)
    except:
        session.rollback()
        raise
    else:
        session.commit()


@settings.configure
def update(config, repo_name):
    model.configure(config)
    session = model.session()

    git_gecko = repos.Gecko(config).repo()
    git_wpt = repos.WebPlatformTests(config).repo()

    gh_wpt = gh.GitHub(config["web-platform-tests"]["github"]["token"],
                       config["web-platform-tests"]["repo"]["url"])

    do_update(config, repo_name, session, git_gecko, git_wpt, gh_wpt)


if __name__ == "__main__":
    try:
        update(sys.argv[1])
    except Exception:
        import traceback
        traceback.print_exc()
        if not "--no-debug" in sys.argv:
            import pdb
            pdb.post_mortem()
