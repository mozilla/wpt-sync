import os
import shutil

import git
from sqlalchemy.orm import joinedload

import log
from model import (Landing,
                   LandingStatus,
                   PullRequest,
                   Repository,
                   Sync,
                   SyncDirection,
                   UpstreamSync,
                   UpstreamSyncStatus,
                   WptCommit,
                   get_or_create)
from projectutil import Mach
from worktree import ensure_worktree


logger = log.get_logger(__name__)


def wpt_push(session, git_wpt, gh_wpt, commits):
    # TODO: check ordering here
    git_wpt.remotes.origin.fetch()
    for commit in commits:
        store_commit(session, gh_wpt, commit)


def store_commit(session, gh_wpt, commit_sha):
    wpt_commit, _ = get_or_create(session, WptCommit, rev=commit_sha)
    if not wpt_commit.pr_id:
        # TODO: we can probably do this in the caller, once per PR.
        # TODO: rate limit these requests
        pr_id = gh_wpt.pr_for_commit(commit_sha)
        if pr_id is not None:
            pr, _ = get_or_create(session, PullRequest, id=pr_id)
            wpt_commit.pr = pr
    return wpt_commit


def copy_wpt(config, git_work_wpt, git_work_gecko, rev, message, bug, commits):
    git_work_wpt.git.checkout(rev)

    dest_path = os.path.join(git_work_gecko.working_dir,
                             config["gecko"]["path"]["wpt"])
    shutil.rmtree(dest_path)
    shutil.copytree(git_work_wpt.working_dir, dest_path)

    git_work_gecko.git.add(config["gecko"]["path"]["wpt"], no_ignore_removal=True)

    message = """%i - [wpt-sync] %s, a=testonly

Automatic update from web-platform-tests containing commits:
%s
""" % (bug, message, "\n".join("  %s" % item.rev for item in commits))
    git_work_gecko.git.commit(message=message)


def get_outstanding_syncs(session):
    return (session.query(UpstreamSync)
            .join(Repository)
            .filter(Repository.name != "autoland",
                    ~UpstreamSync.status.in_((UpstreamSyncStatus.merged,
                                              UpstreamSyncStatus.aborted)))
            .order_by(UpstreamSync.id.asc()))


def reapply_local_commits(session, bz, git_gecko, git_work_gecko, syncs):
    for sync in syncs:
        for commit in reversed(sync.gecko_commits):
            try:
                git_work_gecko.git.cherry_pick(
                    git_gecko.cinnabar.hg2git(commit.hexsha, no_commit=True))
            except git.GitCommandError as e:
                logger.error("Failed to reapply rev %s:\n%s" % (sync.rev, e))
                bz.comment(sync.bug,
                           "Landing wpt failed because reapplying commit %s from bug %s failed "
                           "from rev %s failed:\n%s" % (sync.rev, sync.rev, e))
                return False, None

    git_work_gecko.git.commit(amend=True, no_edit=True)

    return True, syncs


def metadata_commit(config, git_gecko, sync):
    branch = sync.gecko_worktree.rsplit("/", 1)[1]
    branch_head = git_gecko.commit(branch)

    # Check if all the changed files are metadata files;
    # if so we have a metadata update, otherwise we don't
    files_changed = branch_head.stats.files.iterkeys()
    if all(os.path.dirname(item) == config["gecko"]["path"]["meta"]
           for item in files_changed):
        return branch_head.hexsha


def add_metadata(config, git_gecko, git_work_gecko, sync):
    metadata_rev = metadata_commit(config, git_gecko, sync)
    if metadata_rev:
        git_work_gecko.git.cherry_pick(metadata_rev)


def manifest_update(git_work_gecko):
    mach = Mach(git_work_gecko.working_dir)
    mach.wpt_manifest_update()
    if git_work_gecko.is_dirty():
        git_work_gecko.git.add("testing/web-platform/meta")
        git_work_gecko.git.commit(amend=True, no_edit=True)


def update_gecko_wpt(config, session, bz, git_gecko, git_work_wpt, git_work_gecko, wpt_rev,
                     message, outstanding_syncs, sync, bug, commits):
    copy_wpt(config, git_work_wpt, git_work_gecko, wpt_rev, message, bug, commits)
    reapply_local_commits(session, bz, git_gecko, git_work_gecko, outstanding_syncs)
    manifest_update(git_work_gecko)
    if sync:
        add_metadata(config, git_gecko, git_work_gecko, sync)


def create_commits(config, session, bz, git_gecko, git_work_wpt, git_work_gecko, bug,
                   landable_commits, outstanding_syncs):
    """Create commits in a Gecko worktree corresponding to the commits in wpt
    we create a single commit per upstream PR, so that it can be linked to the
    bug used to track downstreaming of that PR.

    :param landable_commits:
    :param List outstanding_syncs: List of patches committed to mozilla central or
                                   inbound that must be reapplied in order to land this commit."""

    for pr, commits in landable_commits:
        # For PRs that are not the result of out own sync, check if
        # we have updated metadta
        if not pr:
            # This is a set of commits that landed directly on master. Gonna assume this doesn't
            # affect test metadata for now
            logger.warning("Commits %s landed directly on master and have no associated "
                           "metadata updates" % ",".join(commits))
            for commit in commits:
                success = update_gecko_wpt(config, session, bz, git_gecko, git_work_wpt,
                                           git_work_gecko, commits[-1].rev, pr.title,
                                           outstanding_syncs, None, bug, [commit])
                if not success:
                    return False
        else:
            assert pr.sync
            success = update_gecko_wpt(config, session, bz, git_gecko, git_work_wpt, git_work_gecko,
                                       commits[-1].rev, pr.title, outstanding_syncs, pr.sync,
                                       pr.sync.bug, commits)
            if not success:
                return False

            if pr.sync.direction == "upstream":
                pr.sync.imported = True
    return True


def push_to_inbound(config, bz, git_gecko, git_work_gecko, bug):
    """Push from git_work_gecko to inbound.

    Returns: Tuple of booleans (success, retry)"""
    try:
        ref = git_work_gecko.head.commit.hexsha
        git_gecko.remotes.mozilla.push(
            "%s:%s" % (ref, config["gecko"]["refs"]["mozilla-inbound"].split("/", 1)[1]))
    except git.GitCommandError as e:
        changes = git_gecko.remotes.mozilla.fetch()
        if not changes:
            logger.error("Pushing update to remote failed:\n%s" % e)
            bz.comment(bug, "Pushing update to remote failed:\n%s" % e)
            return False, True
        try:
            git_work_gecko.git.rebase(config["gecko"]["refs"]["mozilla-inbound"])
        except git.GitCommandError as e:
            logger.error("Rebase failed:\n%s" % e)
            bz.comment(bug, "Rebase failed:\n%s" % e)
            return False, False
    return True, False


def get_batch_sync_point(config, git_gecko):
    """Read the last sync point from the batch sync process from mozilla-central"""
    mozilla_data = git_gecko.git.show("%s:testing/web-platform/meta/mozilla-sync" %
                                      config["gecko"]["refs"]["central"])
    keys = {key: value for line in mozilla_data.split("\n")
            for key, value in line.split(": ", 1)}
    return keys["upstream"]


def load_commits(session, git_wpt, prev_commit, new_commit):
    """Load the commits between two PRs into the database, along with their associated PRs

    :param WptCommit prev_commit: The base commit
    :param WptCommit new_commit: The new head commit
    :return: List of tuples (Upstream PR, [Commits])
    :rtype: List
    """

    unlanded = git_wpt.iter_commits("%s..%s" % (prev_commit.rev, new_commit.rev), reverse=True)

    commits_prs = []
    current_pr = None
    for commit in unlanded:
        # TODO: put this outside the loop so we can do a single db access
        wpt_commit = (session.query(WptCommit)
                      .options(joinedload("pr"))
                      .filter(WptCommit.rev == commit.hexsha).first())
        if not wpt_commit:
            wpt_commit = store_commit(session, gh_wpt, commit)
        if not commits_prs or wpt_commit.pr != current_pr:
            current_pr = wpt_commit.pr
            commits_prs.append((current_pr, []))
        commits_prs[-1][1].append(wpt_commit)

    return commits_prs


def land_to_gecko(config, session, git_gecko, git_wpt, gh_wpt, bz, commit_rev):
    in_progress = Landing.current(session)
    if in_progress:
        assert in_progress.head_commit is not None
        if commit_rev != in_progress.head_commit.rev:
            logger.error("Existing attempt to land commits is in progress; aborting")
            return

    if commit_rev is None:
        git_wpt.remotes.origin.fetch()
        commit_rev = git_wpt.commit("origin/master").hexsha

    logger.info("Syncing to commit %s" % commit_rev)

    commit, _ = get_or_create(session, WptCommit, rev=commit_rev)

    if not in_progress:
        landing = Landing(head_commit=commit)
        session.add(landing)
    else:
        landing = in_progress

    prev_landing = Landing.previous(session)
    if prev_landing is None:
        prev_commit = get_or_create(session, WptCommit, get_batch_sync_point(git_wpt))
    else:
        prev_commit = prev_landing.head_commit

    pr_commits = load_commits(session, git_wpt, prev_commit, landing.head_commit)

    if not pr_commits:
        # No new commits, so return without doing anything
        session.rollback()
        return

    # TODO: Clean this up
    landing.status = LandingStatus.have_commits
    session.commit()

    # END OF STEP 1

    landable_commits = []

    new_landed_commit = None

    for pr, commits in pr_commits:
        if pr and not pr.sync:
            # TODO: schedule a downstream sync for this pr
            break
        landable_commits.append((pr, commits))
        new_landed_commit = commits[-1].rev

    if not landable_commits:
        logger.info("No new commits are landable")
        return

    git_work_wpt, branch_name = ensure_worktree(config, session, git_wpt, "web-platform-tests",
                                                None, "landing", prev_landing.head_commit.rev)
    git_work_gecko, branch_name = ensure_worktree(config, session, git_gecko, "gecko", None,
                                                  "landing", config["gecko"]["refs"]["mozilla-inbound"])

    landing.worktree = git_work_gecko.working_dir

    outstanding_syncs = get_outstanding_syncs(session)

    bug_msg = ""

    if outstanding_syncs:
        for sync in outstanding_syncs:
            bug_msg += "Reapplying unlanded commits from bugs:\n%s" % "\n".join(
                "%s (Pull Request %s - %s)" % (sync.bug, sync.pr_id, sync.pr.title))

    bug = bz.new("Update web-platform-tests to %s" % new_landed_commit,
                 bug_msg,
                 "Testing",
                 "web-platform-tests")
    # TODO: set dependent bugs

    # END OF STEP 2

    create_commits(config, session, bz, git_gecko, git_work_wpt, git_work_gecko, bug,
                   landable_commits, outstanding_syncs)

    # END OF STEP 3

    # Need to deal with upstream changing under us; this approach of just fetch and try to rebase is
    # pretty crude
    # TODO: check treestatus
    success, retry = push_to_inbound(config, bz, git_gecko, git_work_gecko, bug)
    while retry:
        success, retry = push_to_inbound(config, bz, git_gecko, git_work_gecko, bug)

    if not success:
        return

    landing.worktree = None
    landing.status = LandingStatus.complete

    # END OF STEP 4

    # TODO: move this somewhere more sensible
    # Clean up worktrees
    shutil.rmtree(git_work_wpt.working_dir)
    git_wpt.git.worktree("prune")
    shutil.rmtree(git_work_gecko.working_dir)
    git_gecko.git.worktree("prune")
