import os
import re
import subprocess
import sys
import time
import traceback
import urlparse
import uuid
from collections import defaultdict

import git
import gh
import github
from mozautomation import commitparser
from sqlalchemy.orm import joinedload

import log
import model
import settings
from model import Sync, SyncDirection, Commit, Repository, session_scope
from worktree import ensure_worktree, remove_worktrees

logger = log.get_logger("upstream")


def get_backouts(repo, commits):
    # Handle closing PRs with no associated commits left later
    commit_revs = {item.hexsha for item in commits}
    # Mapping from backout rev to list of commits it backs out
    backout_commits = {}

    for commit in commits:
        if commitparser.is_backout(commit.message):
            nodes_bugs = commitparser.parse_backouts(commit.message)
            logger.debug("Commit %s is a backout for %s" %
                         (commit, nodes_bugs))
            if nodes_bugs is None:
                # We think this a backout, but have no idea what it backs out
                # it's not clear how to handle that case so for now we pretend it isn't
                # a backout
                continue

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
                                patch=True, binary=True, format="format:").strip()
    return data == ""


def syncs_from_commits(session, git_gecko, commit_shas):
    hg_revs = [git_gecko.cinnabar.git2hg(git_rev) for git_rev in commit_shas]
    return session.query(Sync).join(Commit).filter(Commit.rev.in_(hg_revs))


def update_for_backouts(session, git_gecko, revs_by_backout):
    backout_revs = set()
    for backout in revs_by_backout.itervalues():
        backout_revs |= set(backout)

    syncs = syncs_from_commits(session, git_gecko, backout_revs)
    rv = []

    logger.debug("Backout revs: %s" % " ".join(backout_revs))

    # Remove the commits from the database
    for sync in syncs:
        modified = len(sync.commits) > 0
        for commit in sync.commits:
            if git_gecko.cinnabar.hg2git(commit.rev) in backout_revs:
                session.delete(commit)
        rv.append((sync, modified))

    return rv


def pr_url(config, pr_id):
    repo_url = config["web-platform-tests"]["repo"]["url"]
    if not repo_url.endswith("/"):
        repo_url += "/"
    return urlparse.urljoin(repo_url, "pulls/%s" % pr_id)


def abort_sync(config, gh_wpt, bz, sync):
    if sync.pr:
        gh_wpt.close_pull(sync.pr)
        bz.comment(sync.bug, "Closed web-platform-tests PR due to backout")

    sync.closed = True


def group_by_bug(git_gecko, commits):
    rv = defaultdict(list)
    for commit in commits:
        bugs = commitparser.parse_bugs(commit.message)
        if len(bugs) > 1:
            logger.warning("Got multiple bugs for commit %s: %s" %
                           (git_gecko.cinnabar.git2hg(commit.hexsha),  ", ".join(bugs)))
        bug = bugs[0]
        rv[bug].append(commit)
    return rv


def update_sync_commits(session, git_gecko, repo_name, commits_by_bug):
    """Update the list of commits for each sync, based on those found in the latest set of
    changes, and return a list of syncs that need to be updated on the wpt side"""

    updated = []

    for bug, commits in commits_by_bug.iteritems():
        if not commits:
            logger.error("No commits for bug %s when trying to update" % bug)
            continue

        sync = (session.query(Sync)
                .options(joinedload(Sync.commits))
                .filter(Sync.bug==bug, Sync.pr_merged==False)).first()

        if sync is None:
            sync = Sync(bug=bug, repository=Repository.by_name(session, repo_name),
                        direction=SyncDirection.upstream)
            session.add(sync)

        if not sync.commits:
            # This is either new or backed out and relanded,
            # so create a new set of commits
            for commit in commits:
                commit = Commit(rev=git_gecko.cinnabar.git2hg(commit.hexsha))
                sync.commits.append(commit)
                session.add(commit)
            modified = True
        else:
            new_commits = [git_gecko.cinnabar.git2hg(item.hexsha) for item in commits]
            prev_commits = [item.rev for item in sync.commits]

            modified = new_commits != prev_commits

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

        updated.append((sync, modified))

        return updated


def move_commit(config, git_gecko, git_work, bz, sync, commit):
    rev = git_gecko.cinnabar.hg2git(commit.rev)
    try:
        # git show is used here rather than git format-patch because it does
        # what we want with merge commits i.e. just generates the patch for
        # the merge resolution, rather than the complete change from one branch
        # or another.
        patch = git_gecko.git.show(rev, "--", config["gecko"]["path"]["wpt"],
                                   pretty="email") + "\n"
    except git.GitCommandError as e:
        logger.error("Failed to create patch from rev %s:\n%s" % (rev, e))
        bz.comment(sync.bug,
                   "Upstreaming to web-platform-tests failed because creating patch "
                   "from rev %s failed:\n%s" % (rev, e))
        return False

    strip_dirs = len(config["gecko"]["path"]["wpt"].split("/")) + 1
    try:
        proc = git_work.git.am("-p%s" % str(strip_dirs), "-",
                               istream=subprocess.PIPE,
                               as_process=True)
        stdout, stderr = proc.communicate(patch)
        if proc.returncode != 0:
            raise git.GitCommandError(["am", "-p%s" % strip_dirs, "-"],
                                      proc.returncode, stderr, stdout)
    except git.GitCommandError as e:
        logger.error("Failed to import patch upstream %s:\n\n%s\n\n%s" % (rev, patch, e))
        bz.comment(sync.bug,
                   "Upstreaming to web-platform-tests failed because applying patch "
                   "from rev %s to upstream failed:\n%s" % (rev, e))
        return False

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
    return True


def create_pr(config, gh_wpt, bz, sync, msg):
    bugzilla_url = ("https://bugzilla.mozilla.org/show_bug.cgi?id=%s" % sync.bug
                    if sync.bug else None)

    while not gh_wpt.get_branch(sync.wpt_branch):
        logger.debug("Waiting for branch")
        time.sleep(1)

    pr_body = "Upstreamed from " + bugzilla_url if bugzilla_url else "Upstreamed from gecko"
    pr = gh_wpt.create_pull(title="[Gecko%s] %s" % (" bug %s" % sync.bug if sync.bug else "",
                                                    msg.splitlines()[0]),
                            body=pr_body,
                            base="master",
                            head=sync.wpt_branch)
    sync.pr = pr
    # TODO: add label to bug
    bz.comment(sync.bug,
               "Created web-platform-tests PR %s for changes under testing/web-platform/tests" %
               pr_url(config, sync.pr))


def update_pr(config, session, git_gecko, git_wpt, gh_wpt, bz, sync):
    git_work, branch_name = ensure_worktree(config, session, git_wpt, "web-platform-tests", sync,
                                            str(sync.bug), "origin/master")
    git_work.index.reset("origin/master", hard=True)

    for commit in sync.commits:
        success = move_commit(config, git_gecko, git_work, bz, sync, commit)
        if not success:
            logger.error("Moving commits failed, skipping further commits in this patch")
            return False

    if not sync.wpt_branch:
        sync.wpt_branch = "gecko_%s_%s" % (branch_name, uuid.uuid4())

    # TODO: maybe if there's >1 commit use the bug title in the PR title
    msg = git_work.commit("HEAD").message

    logger.info("Pushing commits from bug %s to branch %s" % (sync.bug, sync.wpt_branch))
    git_wpt.remotes["origin"].push("%s:%s" % (branch_name, sync.wpt_branch), force=True)

    if not sync.pr:
        create_pr(config, gh_wpt, bz, sync, msg)
    else:
        bz.comment(sync.bug, "Updated web-platform-tests PR")


def update_syncs(config, session, git_gecko, git_wpt, gh_wpt, bz, syncs):
    git_wpt.remotes.origin.fetch()
    # Ensure that any worktrees that got deleted are not in the git config
    git_wpt.git.worktree("prune")

    for sync, modified in syncs:
        if not modified:
            # In theory nothing changed here, so we should be all good
            logger.info("No changes to commits for bug %i" % sync.bug)
            continue

        logger.debug("Sync for bug %i has %i commits" % (sync.bug, len(sync.commits)))
        if not sync.commits:
            abort_sync(config, gh_wpt, bz, sync)
        else:
            success = update_pr(config, session, git_gecko, git_wpt, gh_wpt, bz, sync)
            if not success:
                continue

            gh_wpt.set_status(sync.pr,
                              "failure",
                              target_url=bugzilla_url,
                              description="Landed on mozilla-central",
                              context="upstream/gecko")


def try_land_pr(config, gh_wpt, bz, sync):
    failed_checks = not gh_wpt.status_checks_pass(sync.pr)
    msg = None

    if failed_checks:
        failed_str = "\n".join("%s %s (%s)" %
                               (item.state, item.description, item.target_url)
                               for item in failed)
        logger.warning("Failed to land PR %s because of failed status checks:\n%s" %
                       (sync.pr, failed_str))
        msg = ("Can't merge web-platform-tests PR due to failing status checks:\n%s" %
               failed_str)
    else:
        if not gh_wpt.is_mergeable(sync.pr):
            logger.warning("Failed to land PR %s because it isn't mergeable" % sync.pr)
            msg = "Can't merge web-platform-tests PR"
        else:
            try:
                gh_wpt.merge_pull(sync.pr)
            except github.GithubException as e:
                logger.error("Merging PR %s failed:\n%s" % (sync.pr, e))
                msg = "Merging web-platform-tests PR failed"
            else:
                msg = ("Merged associated web-platform-tests PR. "
                       "Thanks for writing web-platform-tests!")
                sync.pr_merged = True
                remove_worktrees(config, sync)

    if msg is not None:
        bz.comment(sync.bug, msg)


def land_syncs(config, session, git_gecko, git_wpt, gh_wpt, bz, syncs):
    # Ensure that any worktrees that got deleted are not in the git config
    git_wpt.git.worktree("prune")

    for sync, modified in syncs:
        if not sync.commits:
            if modified:
                # It's not really clear this should happen unless we miss a push to
                # a landing repo or something
                abort_sync(config, gh_wpt, bz, sync)
        else:
            if modified or not sync.pr:
                success = update_pr(config, session, git_gecko, git_wpt, gh_wpt, bz, sync)
                if not success:
                    continue

            gh_wpt.set_status(sync.pr,
                              "success",
                              target_url=None,
                              description=None,
                              context="upstream/gecko")

            try_land_pr(config, gh_wpt, bz, sync)


def get_wpt_commits(config, git_gecko, revish):
    gecko_wpt_path = config["gecko"]["path"]["wpt"]

    all_commits = list(git_gecko.iter_commits(revish))

    if all_commits:
        logger.debug("Found %i new commits from central starting at %s" %
                     (len(all_commits), all_commits[0].hexsha))
    else:
        logger.debug("No new commits since central")

    wpt_commits = [item.hexsha for item in
                   git_gecko.iter_commits(revish, paths=gecko_wpt_path)]

    if not wpt_commits:
        logger.info("No commits updated %s" % gecko_wpt_path)

    return all_commits, wpt_commits


def filter_backouts(git_gecko, all_commits, wpt_commits):
    """Remove backouts. This must be done across all commits
    # so that we can tell if a backout only touches things in the set
    # we are considering"""
    backout_revs, no_backout_commits = get_backouts(git_gecko, all_commits)

    # Now filter down to just wpt commits with backouts removed
    commits = [item for item in no_backout_commits if item.hexsha in wpt_commits]

    if commits:
        logger.info("%i commits remain after removing backouts" % (len(commits),))
    else:
        logger.info("No commits remaining after removing backouts")

    return backout_revs, commits


def filter_empty(gecko_wpt_path, commits):
    """Remove empty commits (probably merges)"""
    commits = [item for item in commits if not is_empty(item, gecko_wpt_path)]

    if commits:
        logger.info("%i commits remain after removing empty commits" % (len(commits),))
    else:
        logger.info("No commits remaining after removing empty commits")
    return commits


def syncs_for_push(config, session, git_gecko, git_wpt, repository, first_commit):
    gecko_wpt_path = config["gecko"]["path"]["wpt"]

    # List of syncs that have changed, so we can update them all as appropriate at the end
    modified_syncs = []

    if repository.name == "autoland":
        modified_syncs.extend(remove_missing_commits(config, session, git_gecko, repository))

    if first_commit:
        revish = "%s..%s" % (first_commit,
                             config["gecko"]["refs"][repository.name])
    else:
        revish = config["gecko"]["refs"][repository.name]

    all_commits, wpt_commits = get_wpt_commits(config, git_gecko, revish)

    logger.info("Found %i commits affecting wpt: %s" % (len(wpt_commits), " ".join(wpt_commits)))
    wpt_commits = set(wpt_commits)

    backout_revs, commits = filter_backouts(git_gecko, all_commits, wpt_commits)
    commits = filter_empty(gecko_wpt_path, commits)

    modified_syncs.extend(update_for_backouts(session, git_gecko, backout_revs))

    # TODO: check autoland to ensure that there aren't any commits that no longer exist in git
    # on syncs

    if commits:
        by_bug = group_by_bug(git_gecko, commits)

        # There is one worrying case we don't handle yet. What if a bug lands in central,
        # but before we mark the commits as on-central, we get a new commit for the same bug
        # In that case we end up overwriting the old commits.
        modified_syncs.extend(update_sync_commits(session, git_gecko, repository.name, by_bug))

    return modified_syncs


def remove_missing_commits(config, session, git_gecko, repository):
    """Remove any commits from the database that are no longer in the source
    repository"""
    commits = session.query(Commit).join(Sync).filter(Sync.repository == repository)
    upstream = config["gecko"]["refs"][repository.name]
    syncs = set()

    for commit in commits:
        status, _, _ = git_gecko.git.merge_base(upstream,
                                                git_gecko.cinnabar.hg2git(commit.rev),
                                                is_ancecstor=True,
                                                with_extended_output=True,
                                                with_exceptions=False)
        if status != 0:
            logger.debug("Removed commit %s that was deleted upstream" % commit.rev)
            syncs.add(commit.sync)
            session.delete(commit)

    return syncs


def update_gecko(git_gecko, repository):
    logger.info("Fetching mozilla-unified")
    # Not using the built in fetch() function since that tries to parse the output
    # and sometimes fails
    git_gecko.git.fetch("mozilla")
    logger.info("Fetch done")

    if repository.name == "autoland":
        logger.info("Fetch autoland")
        git_gecko.git.fetch("autoland")
        logger.info("Fetch done")


def integration_commit(config, session, git_gecko, git_wpt, gh_wpt, bz, repo_name):
    """Update PRs for a new commit to an integration repository such as
    autoland or mozilla-inbound."""
    repository, _ = model.get_or_create(session, model.Repository, name=repo_name)

    update_gecko(git_gecko, repository)

    first_commit = config["gecko"]["refs"]["central"]

    # This blind rollback-on-exception approach means that the db is
    # likely to get out of sync with external data stores
    with session_scope(session):
        syncs = syncs_for_push(config, session, git_gecko, git_wpt,
                               repository, first_commit)

        update_syncs(config, session, git_gecko, git_wpt, gh_wpt, bz, syncs)


def landing_commit(config, session, git_gecko, git_wpt, gh_wpt, bz):

    repository, _ = model.get_or_create(session, model.Repository, name="central")

    update_gecko(git_gecko, repository)

    last_sync_commit = repository.last_processed_commit_id
    if last_sync_commit is None:
        merge_commits = git_gecko.iter_commits(config["gecko"]["refs"]["central"], min_parents=2)
        merge_commits.next()
        try:
            prev_merge = merge_commits.next()
            last_sync_commit = prev_merge.hexsha
        except StopIteration:
            # This is really only needed for tests
            last_sync_commit = ""

    commits = git_gecko.iter_commits(config["gecko"]["refs"]["central"])
    repository.last_processed_commit_id = commits.next().hexsha

    with session_scope(session):
        syncs = syncs_for_push(config, session, git_gecko, git_wpt,
                               repository, last_sync_commit)
        for sync, _ in syncs:
            sync.repository = repository
        land_syncs(config, session, git_gecko, git_wpt, gh_wpt, bz, syncs)


def status_changed(config, session, bz, git_gecko, git_wpt, gh_wpt, sync, context, status, url):
    if status == "pending":
        # Never change anything for pending
        return

    if status == "passed":
        if gh_wpt.status_checks_pass(sync.pr):
            if sync.repository.name != "central":
                land_syncs(config, session, git_gecko, git_wpt, gh_wpt, bz, [sync])
            else:
                bz.add_comment(sync.bug, "Upstream web-platform-tests status checked passed, "
                               "PR will merge once commit reaches central.")
    else:
        bz.add_comment(sync.bug, "Upstream web-platform-tests status %s for %s. "
                       "This will block the upstream PR from merging. "
                       "See %s for more information" % (status, context, url))


@settings.configure
def main(config):
    import handlers
    session, git_gecko, git_wpt, gh_wpt, bz = handlers.setup(config)
    try:
        repo_name = sys.argv[1]
        if repo_name in ("mozilla-inbound", "autoland"):
            integration_commit(config, session, git_gecko, git_wpt, gh_wpt, bz, repo_name)
        elif repo_name == "central":
            landing_commit(config, session, git_gecko, git_wpt, gh_wpt, bz)
        else:
            print >> sys.stderr, "Unrecognised repo %s" % repo_name
    except Exception:
        traceback.print_exc()
        if "--no-debug" not in sys.argv:
            import pdb
            pdb.post_mortem()


if __name__ == "__main__":
    main()
