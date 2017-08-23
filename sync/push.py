import os
import shutil

import git
from sqlalchemy.orm import joinedload

import log
from model import (Landing,
                   PullRequest,
                   Repository,
                   Sync,
                   SyncDirection,
                   WptCommit,
                   get_or_create)
from projectutil import Mach
from worktree import ensure_worktree


logger = log.get_logger(__name__)


def get_last_push(session):
    landing, _ = get_or_create(session, Landing)
    return landing.last_push_commit


def wpt_push(session, git_wpt, gh_wpt):
    # TODO: check ordering here

    last_push_commit = get_last_push(session)

    git_wpt.remotes.origin.fetch()
    for commit in git_wpt.iter_commits("%s..origin/master" % last_push_commit, reverse=True):
        store_commit(session, gh_wpt, commit)
    landing, _ = get_or_create(session, Landing)
    landing.last_push_commit = git_wpt.commit("origin/master").hexsha


def store_commit(session, gh_wpt, commit):
    wpt_commit, _ = get_or_create(session, WptCommit, rev=commit.hexsha)
    if not wpt_commit.pr_id:
        # TODO: rate limit these requests
        pr_id = gh_wpt.pr_for_commit(commit.hexsha)
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
    return (session.query(Sync)
            .join(Repository)
            .filter(Sync.direction == SyncDirection.upstream,
                    Repository.name != "autoland",
                    Sync.imported.is_(False),
                    Sync.closed.is_(False))
            .order_by(Sync.id.asc()))


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
    mach = Mach(git_work_gecko)
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
        elif not pr.sync:
            # TODO: Need to ensure that we start the downstreaming process here
            break
        else:
            success = update_gecko_wpt(config, session, bz, git_gecko, git_work_wpt, git_work_gecko,
                                       commits[-1].rev, pr.title, outstanding_syncs, pr.sync,
                                       pr.sync.bug, commits)
            if not success:
                return False

            if pr.sync.direction == SyncDirection.upstream:
                pr.sync.imported = True
            elif pr.sync.direction == SyncDirection.downstream:
                # TODO: check if we have updated metadata
                pass
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


def land_to_gecko(config, session, git_gecko, git_wpt, gh_wpt, bz):
    git_wpt.remotes.origin.fetch()
    landing, _ = get_or_create(session, Landing)

    if landing.worktree:
        logger.error("Existing attempt to land commits is in progress; aborting")
        return

    last_landed = landing.last_landed_commit
    if not last_landed:
        logger.error("Tried to process a push, but no previous sync point found")
        return

    unlanded = git_wpt.iter_commits("%s..origin/master" % last_landed, reverse=True)

    commits_prs = []
    current_pr = None
    for commit in unlanded:
        wpt_commit = (session.query(WptCommit)
                      .options(joinedload("pr"))
                      .filter(WptCommit.rev == commit.hexsha).first())
        if not wpt_commit:
            wpt_commit = store_commit(session, gh_wpt, commit)
        if not commits_prs or wpt_commit.pr != current_pr:
            current_pr = wpt_commit.pr
            commits_prs.append((current_pr, []))
        commits_prs[-1][1].append(wpt_commit)

    if not commits_prs:
        return

    landable_commits = []

    new_landed_commit = None

    for pr, commits in commits_prs:
        if pr and not pr.sync:
            break
        if pr.sync.direction == SyncDirection.upstream:
            pr.sync.imported = True
        landable_commits.append((pr, commits))
        new_landed_commit = commits[-1].rev

    if not landable_commits:
        logger.info("No new commits are landable")
        return

    git_work_wpt, branch_name = ensure_worktree(config, session, git_wpt, "web-platform-tests",
                                                None, "landing", last_landed)
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
    create_commits(config, session, bz, git_gecko, git_work_wpt, git_work_gecko, bug,
                   landable_commits, outstanding_syncs)

    # Need to deal with upstream changing under us; this approach of just fetch and try to rebase is
    # pretty crude
    # TODO: check treestatus
    success, retry = push_to_inbound(config, bz, git_gecko, git_work_gecko, bug)
    while retry:
        success, retry = push_to_inbound(config, bz, git_gecko, git_work_gecko, bug)

    if not success:
        return

    landing.worktree = None
    landing.last_landed_commit = new_landed_commit

    # TODO: move this somewhere more sensible
    # Clean up worktrees
    shutil.rmtree(git_work_wpt.working_dir)
    git_wpt.git.worktree("prune")
    shutil.rmtree(git_work_gecko.working_dir)
    git_gecko.git.worktree("prune")
