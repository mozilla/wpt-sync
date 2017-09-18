import os
import shutil

import git
from sqlalchemy.orm import joinedload

import log
from model import (Landing,
                   PullRequest,
                   Status,
                   SyncDirection,
                   UpstreamSync,
                   WptCommit,
                   get_or_create)
from projectutil import Mach
from worktree import ensure_worktree
from pipeline import pipeline, step, AbortError

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
                raise AbortError

    git_work_gecko.git.commit(amend=True, no_edit=True)


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
            raise AbortError
        try:
            git_work_gecko.git.rebase(config["gecko"]["refs"]["mozilla-inbound"])
        except git.GitCommandError as e:
            logger.error("Rebase failed:\n%s" % e)
            bz.comment(bug, "Rebase failed:\n%s" % e)
            raise AbortError
        return False
    return True


def get_batch_sync_point(config, git_gecko):
    """Read the last sync point from the batch sync process from mozilla-central"""
    mozilla_data = git_gecko.git.show("%s:testing/web-platform/meta/mozilla-sync" %
                                      config["gecko"]["refs"]["central"])
    keys = {key: value for line in mozilla_data.split("\n")
            for key, value in line.split(": ", 1)}
    return keys["upstream"]


def load_commits(session, git_wpt, gh_wpt, prev_commit, new_commit):
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


@step()
def get_landing(config, session, git_gecko, git_wpt, gh_wpt, bz):
    landing = Landing.current(session)

    if not landing:
        git_wpt.remotes.origin.fetch()
        commit, _ = get_or_create(session,
                                  WptCommit,
                                  rev=git_wpt.commit("origin/master").hexsha)
        landing = Landing(head_commit=commit)
        session.add(landing)
        logger.info("Syncing to commit %s" % commit.rev)

    git_wpt.remotes.origin.fetch(landing.head_commit.rev)

    prev_landing = Landing.previous(session)
    if prev_landing is None:
        prev_commit = get_or_create(session, WptCommit, get_batch_sync_point(git_wpt))
    else:
        prev_commit = prev_landing.head_commit

    pr_commits = load_commits(session, git_wpt, gh_wpt, prev_commit, commit)

    if not pr_commits:
        logger.info("No commits to land")
        raise AbortError(cleanup=[landing])

    return landing, prev_landing, pr_commits


@step(state_arg="landing")
def get_landable_commits(config, session, git_gecko, git_wpt, gh_wpt, bz,
                         landing, prev_landing, pr_commits):
    landable_commits = []

    for pr, commits in pr_commits:
        if pr and not (pr.sync and (pr.sync.direction == SyncDirection.upstream or
                                    pr.sync.metadata_ready)):
            # TODO: schedule a downstream sync for this pr
            logger.info("Not landing PR %s or after" % pr.id)
            break
        landable_commits.append((pr, commits))

    if not landable_commits:
        logger.info("No new commits are landable")
        raise AbortError(cleanup=[landing])

    landing.head_commit = landable_commits[-1][1][-1]

    logger.info("Landing up to commit %s" % landing.head_commit.rev)

    return landable_commits


@step(state_arg="landing")
def setup_worktrees(config, session, git_wpt, git_gecko, landing, prev_landing, has_modified_syncs):
    if landing.wpt_worktree and landing.gecko_worktree and not has_modified_syncs:
        # If we are redoing a landing, and syncs have not changed,
        # start from the existing branch
        wpt_base = landing.wpt_worktree.rsplit("/", 1)[1]
        gecko_base = landing.gecko_worktree.rsplit("/", 1)[1]
    else:
        wpt_base = prev_landing.head_commit.rev
        gecko_base = config["gecko"]["refs"]["mozilla-inbound"]

    git_work_wpt, _, created = ensure_worktree(config,
                                               session,
                                               git_wpt,
                                               "web-platform-tests",
                                               None,
                                               "landing",
                                               wpt_base)
    git_work_gecko, _, created = ensure_worktree(config,
                                                 session,
                                                 git_gecko,
                                                 "gecko",
                                                 None,
                                                 "landing",
                                                 gecko_base)

    landing.wpt_worktree = git_work_wpt.working_dir
    landing.gecko_worktree = git_work_gecko.working_dir

    if not created and has_modified_syncs:
        # If we need to start the landing again e.g. due to more
        # upstream syncs, we might already have a worktree, but it
        # might have the wrong thing checked out. So explicitly
        # reset it here.
        git_work_wpt.head.reset(wpt_base, working_tree=True)
        git_work_gecko.head.reset(gecko_base, working_tree=True)

    return git_work_wpt, git_work_gecko


@step(state_arg="landing",
      skip_if=lambda x: x.bug)
def create_bug(config, session, bz, landing, landable_commits):
    last_landed_commit = landable_commits[-1][1][-1]
    bug_msg = ""

    for sync in landing.syncs_reapplied:
        bug_msg += "Reapplying unlanded commits from bugs:\n%s" % "\n".join(
            "%s (Pull Request %s - %s)" % (sync.bug, sync.pr_id, sync.pr.title))

    if not landing.bug:
        # We might already have a bug if we had to start again with different syncs
        bug = bz.new("Update web-platform-tests to %s" % last_landed_commit,
                     bug_msg,
                     "Testing",
                     "web-platform-tests")
        landing.bug = bug
    else:
        bug_msg = "Landing was restarted due to changes in gecko trees.\n%s" % bug_msg
        bz.comment(landing.bug, bug_msg)


@step()
def get_outstanding_syncs(config, session, landing):
    syncs = UpstreamSync.unlanded(session)
    # We are either running this for the first time, or we
    # are running again but got different syncs to apply this time
    # so need to re-do all the following steps
    has_modified_syncs = syncs.all() != landing.syncs_reapplied
    landing.syncs_reapplied = syncs.all()
    return has_modified_syncs


@step()
def reset_commit_landing(config, session, landing, commits):
    for commit in commits:
        if commit.landing is not None:
            assert commit.landing == landing
            commit.landing = None


@step(state_arg="landing",
      skip_if=lambda x: x.commits_applied)
def create_commits(config, session, bz, git_gecko, git_work_wpt, git_work_gecko,
                   landing, landable_commits):
    """Create commits in a Gecko worktree corresponding to the commits in wpt
    we create a single commit per upstream PR, so that it can be linked to the
    bug used to track downstreaming of that PR.

    :param landable_commits:
    """
    for pr, commits in landable_commits:
        move_pr_commits(config, session, bz, git_gecko, git_work_wpt, git_work_gecko,
                        landing, pr, commits)
    landing.commits_applied = True


@step()
def move_pr_commits(config, session, bz, git_gecko, git_work_wpt, git_work_gecko,
                    landing, pr, commits):

    # If we already applied these commits to the landing, abort
    if commits[0].landing:
        assert all(item.landing == landing for item in commits)
        return

    # For PRs that are not the result of out own sync, check if
    # we have updated metadta
    if not pr:
        # This is a set of commits that landed directly on master. Gonna assume this doesn't
        # affect test metadata for now
        logger.warning("Commits %s landed directly on master and have no associated "
                       "metadata updates" % ",".join(commits))
        for commit in commits:
            update_gecko_wpt(config, session, bz, git_gecko, git_work_wpt,
                             git_work_gecko, commits[-1].rev, pr.title,
                             landing.syncs_reapplied, None, landing.bug, [commit])
    else:
        assert pr.sync
        update_gecko_wpt(config, session, bz, git_gecko, git_work_wpt, git_work_gecko,
                         commits[-1].rev, pr.title, landing.syncs_reapplied, pr.sync,
                         pr.sync.bug, commits)
    for commit in commits:
        commit.landing = landing


@step(state_arg="landing",
      skip_if=lambda x: x.status == Status.complete)
def push_commits(config, session, bz, git_gecko, git_work_gecko, landing):
    # Need to deal with upstream changing under us; this approach of just fetch and try to rebase is
    # pretty crude
    # TODO: check treestatus
    while not push_to_inbound(config, bz, git_gecko, git_work_gecko, landing.bug):
        # Retry whilst we try to rebase onto latest inbound
        pass

    landing.status = Status.complete
    landing.wpt_worktree = None
    landing.gecko_worktree = None


@step()
def mark_syncs_completed(config, session, pr_commits):
    for pr, _ in pr_commits:
        if pr.sync.direction == SyncDirection.downstream:
            pr.sync.status = Status.complete
        if pr.sync.direction == SyncDirection.upstream:
            pr.sync.status = Status.complete


@pipeline
def land_to_gecko(config, session, git_gecko, git_wpt, gh_wpt, bz):

    landing, prev_landing, pr_commits = get_landing(config, session, git_gecko,
                                                    git_wpt, gh_wpt, bz)

    landable_commits = get_landable_commits(config, session, git_gecko, git_wpt, gh_wpt, bz,
                                            landing, prev_landing, pr_commits)

    has_modified_syncs = get_outstanding_syncs(config, session, landing)

    if has_modified_syncs:
        # If there are modified commits, ensure that any commits already
        # marked as applied to this landing are unmarked so that we start again
        reset_commit_landing(landable_commits)

    create_bug(config, session, bz, landing, landable_commits)

    git_work_wpt, git_work_gecko = setup_worktrees(config, session, git_wpt, git_gecko,
                                                   landing, prev_landing, has_modified_syncs)

    create_commits(config, session, bz, git_gecko, git_work_wpt, git_work_gecko, landing,
                   landable_commits)

    push_commits(config, session, bz, git_gecko, git_work_gecko, landing)

    mark_syncs_completed(config, session, pr_commits)

    # TODO: move this somewhere more sensible
    # Clean up worktrees
    shutil.rmtree(git_work_wpt.working_dir)
    git_wpt.git.worktree("prune")
    shutil.rmtree(git_work_gecko.working_dir)
    git_gecko.git.worktree("prune")
