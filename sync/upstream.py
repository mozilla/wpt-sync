import re
import subprocess
import sys
import time
import traceback
import urlparse
import uuid
from collections import defaultdict

import git
from mozautomation import commitparser
from sqlalchemy.orm import joinedload

import log
import model
import settings
from model import (Sync,
                   UpstreamSync,
                   DownstreamSync,
                   SyncSubclass,
                   Status,
                   GeckoCommit,
                   PullRequest,
                   Repository)
from gitutils import is_ancestor
from pipeline import pipeline, step, AbortError, MultipleExceptions
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
    return session.query(UpstreamSync).join(GeckoCommit).filter(GeckoCommit.rev.in_(hg_revs))


def update_for_backouts(session, git_gecko, revs_by_backout):
    backout_revs = set()
    for backout in revs_by_backout.itervalues():
        backout_revs |= set(backout)

    syncs = syncs_from_commits(session, git_gecko, backout_revs)
    rv = []

    logger.debug("Backout revs: %s" % " ".join(backout_revs))

    # Remove the commits from the database
    for sync in syncs:
        modified = len(sync.gecko_commits) > 0
        for commit in sync.gecko_commits:
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
        gh_wpt.close_pull(sync.pr_id)
        bz.comment(sync.bug, "Closed web-platform-tests PR due to backout")

    sync.status = Status.aborted


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

        sync = (session.query(SyncSubclass)
                .options(joinedload(SyncSubclass.pr),
                         joinedload(SyncSubclass.UpstreamSync.gecko_commits))
                .filter(Sync.bug == bug)).first()

        if sync and isinstance(sync, DownstreamSync):
            # This is from downstreaming, so skip the commits
            continue

        if sync is None or sync.pr.merged:
            # This is either something we haven't seen before or something that
            # we have previously merged, but that got additional commits
            sync = UpstreamSync(bug=bug,
                                repository=Repository.by_name(session, repo_name))
            session.add(sync)

        if not sync.gecko_commits:
            # This is either new or backed out and relanded,
            # so create a new set of commits
            for commit in commits:
                commit = GeckoCommit(rev=git_gecko.cinnabar.git2hg(commit.hexsha))
                sync.gecko_commits.append(commit)
                session.add(commit)
            modified = True
        else:
            new_commits = [git_gecko.cinnabar.git2hg(item.hexsha) for item in commits]
            prev_commits = [item.rev for item in sync.gecko_commits]

            modified = new_commits != prev_commits

            if not new_commits[:len(prev_commits)] == prev_commits:
                # The commits changed, so we want to do something sensible
                # There are a couple of possibilities:
                #  * The commits for the bug changed because of a history
                #    rewrite on autoland. In this case we can just replace the old commits
                #    with the new ones and keep going
                #  * The old commits made it to mozilla-central and so aren't in the
                #    loaded set, so we want to append to the existing PR

                # TODO : check if the commits still exist using
                # git merge-base --is-ancecstor upstream/repo_name rev

                prev_revs = set(prev_commits)
                for rev in new_commits:
                    if rev not in prev_revs:
                        sync.gecko_commits.append(GeckoCommit(rev=rev))
            else:
                # More commits were added to a bug that already has some
                for rev in new_commits[len(prev_commits):]:
                    sync.gecko_commits.append(GeckoCommit(rev=rev))

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


def bugzilla_url(sync):
    return ("https://bugzilla.mozilla.org/show_bug.cgi?id=%s" % sync.bug
            if sync.bug else None)


@step(state_arg="sync",
      skip_if=lambda x: x.pr)
def create_pr(config, session, git_gecko, gh_wpt, bz, sync):
    while not gh_wpt.get_branch(sync.wpt_branch):
        logger.debug("Waiting for branch")
        time.sleep(1)

    link = bugzilla_url(sync)
    pr_body = "Upstreamed from %s" % link if link else "Upstreamed from gecko"
    msg = git_gecko.commit(
        git_gecko.cinnabar.hg2git(sync.gecko_commits[0].rev)).message.splitlines()[0]
    pr = gh_wpt.create_pull(title="[Gecko%s] %s" % (" bug %s" % sync.bug if sync.bug else "", msg),
                            body=pr_body,
                            base="master",
                            head=sync.wpt_branch)
    sync.pr = PullRequest(id=pr)
    # TODO: add label to bug
    bz.comment(sync.bug,
               "Created web-platform-tests PR %s for changes under testing/web-platform/tests" %
               pr_url(config, sync.pr_id))


@step(state_arg="sync",
      skip_if=lambda x: x.commits_applied)
def apply_commits(config, session, git_gecko, git_wpt, bz, sync):
    last_landing = model.Landing.previous(session)
    if last_landing:
        base_commit = last_landing.head_commit.rev
    else:
        base_commit = "origin/master"
    git_work, branch_name, _ = ensure_worktree(config, session, git_wpt, "web-platform-tests", sync,
                                               str(sync.bug), base_commit)
    git_work.index.reset(base_commit, hard=True)

    for commit in sync.gecko_commits:
        success = move_commit(config, git_gecko, git_work, bz, sync, commit)
        if not success:
            logger.error("Moving commits failed, skipping further commits in this patch")
            raise AbortError(set_flag="error_apply_failed")

    sync.error_apply_failed = False

    if not sync.wpt_branch:
        sync.wpt_branch = "gecko_%s_%s" % (branch_name, uuid.uuid4())

    try:
        git_work.git.rebase("origin/master")
    except git.GitCommandError:
        logger.error("Rebasing onto upstream master failed")
        # Don't worry about this here; we need to detect PRs that become
        # unmergable anyway

    sync.commits_applied = True

    return branch_name


@step()
def push_commits(config, session, git_wpt, sync, branch_name):
    # TODO: maybe if there's >1 commit use the bug title in the PR title
    logger.info("Pushing commits from bug %s to branch %s" % (sync.bug, sync.wpt_branch))
    git_wpt.remotes["origin"].push("%s:%s" % (branch_name, sync.wpt_branch), force=True)


@step()
def try_land_pr(config, session, gh_wpt, bz, sync):
    failed_checks = not gh_wpt.status_checks_pass(sync.pr_id)
    msg = None

    if failed_checks:
        failed_str = "\n".join("%s %s (%s)" %
                               (item.state, item.description, item.target_url)
                               for item in failed)
        logger.warning("Failed to land PR %s because of failed status checks:\n%s" %
                       (sync.pr_id, failed_str))
        msg = ("Can't merge web-platform-tests PR due to failing status checks:\n%s" %
               failed_str)
        sync.error_status_failed = True
    else:
        sync.error_status_failed = False
        if not gh_wpt.is_mergeable(sync.pr_id):
            logger.warning("Failed to land PR %s because it isn't mergeable" % sync.pr_id)
            msg = "Can't merge web-platform-tests PR"
            sync.error_unmergable = True
        else:
            sync.error_unmergable = False
            gh_wpt.merge_pull(sync.pr_id)
            # If we get an error here then we want to retry because GitHub is down
            # so let it bubble up through the normal channels
            msg = ("Merged associated web-platform-tests PR. "
                   "Thanks for writing web-platform-tests!")
            sync.pr.merged = True
            sync.status = Status.complete
            remove_worktrees(config, sync)

    if msg is not None:
        bz.comment(sync.bug, msg)


def set_status(gh_wpt, sync, status):
    gh_wpt.set_status(sync.pr_id,
                      status,
                      target_url=bugzilla_url(sync),
                      description="Landed on mozilla-central",
                      context="upstream/gecko")


@step(state_arg="sync",
      skip_if=lambda x: x.status != Status.active)
def update_sync(config, session, git_gecko, git_wpt, gh_wpt, bz, sync, modified):
    if not modified:
        # In theory nothing changed here, so we should be all good
        logger.info("No changes to commits for bug %i" % sync.bug)
        return

    logger.debug("Sync for bug %i has %i commits" % (sync.bug, len(sync.gecko_commits)))
    if not sync.gecko_commits:
        abort_sync(config, gh_wpt, bz, sync)
    else:
        branch_name = apply_commits(config, session, git_gecko, git_wpt, bz, sync)
        push_commits(config, session, git_wpt, sync, branch_name)
        set_status(gh_wpt, sync, "failure")


@step(state_arg="sync",
      skip_if=lambda x: x.status != Status.active)
def land_sync(config, session, git_gecko, git_wpt, gh_wpt, bz, sync, modified):
    if not sync.gecko_commits:
        if modified:
            # It's not really clear this should happen unless we miss a push to
            # a landing repo or something
            abort_sync(config, gh_wpt, bz, sync)
    else:
        if modified or not sync.pr:
            branch_name = apply_commits(config, session, git_gecko, git_wpt, bz, sync)
            push_commits(config, session, git_wpt, sync, branch_name)
        set_status(gh_wpt, sync, "success")

        try_land_pr(config, session, gh_wpt, bz, sync)


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


@step()
def syncs_for_push(config, session, git_gecko, git_wpt, repository, first_commit, head_commit):
    gecko_wpt_path = config["gecko"]["path"]["wpt"]

    # List of syncs that have changed, so we can update them all as appropriate at the end
    modified_syncs = []

    if repository.name == "autoland":
        modified_syncs.extend(remove_missing_commits(config, session, git_gecko, repository))

    head_commit = head_commit if head_commit else config["gecko"]["refs"][repository.name]
    revish = "%s..%s" % (first_commit, head_commit) if first_commit else head_commit

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

    return git_gecko.commit(head_commit), modified_syncs


def remove_missing_commits(config, session, git_gecko, repository):
    """Remove any commits from the database that are no longer in the source
    repository"""
    commits = (session
               .query(GeckoCommit)
               .join(UpstreamSync)
               .filter(UpstreamSync.repository == repository))
    upstream = config["gecko"]["refs"][repository.name]
    syncs = set()

    for commit in commits:
        status, _, _ = git_gecko.git.merge_base(upstream,
                                                git_gecko.cinnabar.hg2git(commit.rev),
                                                is_ancestor=True,
                                                with_extended_output=True,
                                                with_exceptions=False)
        if status != 0:
            logger.debug("Removed commit %s that was deleted upstream" % commit.rev)
            syncs.add(commit.sync)
            session.delete(commit)

    return syncs


@step()
def update_repositories(config, session, git_gecko, git_wpt, repo_name, hg_rev):
    repository, _ = model.get_or_create(session, model.Repository, name=repo_name)

    logger.info("Fetching mozilla-unified")
    # Not using the built in fetch() function since that tries to parse the output
    # and sometimes fails
    git_gecko.git.fetch("mozilla")
    logger.info("Fetch done")

    if repository.name == "autoland":
        logger.info("Fetch autoland")
        git_gecko.git.fetch("autoland")
        logger.info("Fetch done")

    git_wpt.git.fetch("origin")

    return repository, git_gecko.cinnabar.hg2git(hg_rev)


@step()
def get_sync_commit(config, session, git_gecko, repository, rev):
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

    if is_ancestor(git_gecko, rev, last_sync_commit):
        # This commit has already been processed so
        # there's nothing to do
        raise AbortError

    return last_sync_commit


@step()
def update_repos_on_landing(config, session, repository, modified_syncs, new_sync_commit):
    for sync, _ in modified_syncs:
        sync.repository = repository

    repository.last_processed_commit_id = new_sync_commit.hexsha


@pipeline
def integration_commit(config, session, git_gecko, git_wpt, gh_wpt, bz, hg_rev, repo_name):
    """Update PRs for a new commit to an integration repository such as
    autoland or mozilla-inbound."""
    repository, rev = update_repositories(config, session, git_gecko, git_wpt, repo_name, hg_rev)

    # TODO: is this correct if we have a merge from central into inbound?
    first_commit = git_gecko.merge_base(rev, config["gecko"]["refs"]["central"])[0]

    new_sync_commit, syncs = syncs_for_push(config, session, git_gecko, git_wpt,
                                            repository, first_commit.hexsha, rev)

    # TODO: might be good to add an abstraction for this pattern for handling loops
    # of independent things
    exceptions = []
    for sync, modified in syncs:
        try:
            create_pr(config, session, git_gecko, gh_wpt, bz, sync)
            update_sync(config, session, git_gecko, git_wpt, gh_wpt, bz, sync, modified)
        except Exception as e:
            if not isinstance(e, AbortError):
                logger.warning(traceback.format_exc(e))
            exceptions.append(e)
    if exceptions:
        raise MultipleExceptions(exceptions)


@pipeline
def landing_commit(config, session, git_gecko, git_wpt, gh_wpt, bz, hg_rev):
    repository, rev = update_repositories(config, session, git_gecko, git_wpt, "central", hg_rev)
    last_sync_commit = get_sync_commit(config, session, git_gecko, repository, rev)

    new_sync_commit, syncs = syncs_for_push(config, session, git_gecko, git_wpt,
                                            repository, last_sync_commit, rev)

    update_repos_on_landing(config, session, repository, syncs, new_sync_commit)

    # TODO: might be good to add an abstraction for this pattern for handling loops
    # of independent things
    exceptions = []
    for sync, modified in syncs:
        try:
            create_pr(config, session, git_gecko, gh_wpt, bz, sync)
            land_sync(config, session, git_gecko, git_wpt, gh_wpt, bz, sync, modified)
        except Exception as e:
            if not isinstance(e, AbortError):
                logger.warning(traceback.format_exc(e))
            exceptions.append(e)
    if exceptions:
        raise MultipleExceptions(exceptions)


@pipeline
def status_changed(config, session, bz, git_gecko, git_wpt, gh_wpt, sync, context, status, url):
    if status == "pending":
        # Never change anything for pending
        return

    if status == "passed":
        if sync.repository.name == "central":
            land_sync(config, session, git_gecko, git_wpt, gh_wpt, bz, sync)
        else:
            bz.add_comment(sync.bug, "Upstream web-platform-tests status checked passed, "
                           "PR will merge once commit reaches central.")
    else:
        bz.add_comment(sync.bug, "Upstream web-platform-tests status %s for %s. "
                       "This will block the upstream PR from merging. "
                       "See %s for more information" % (status, context, url))


@settings.configure
def main(config):
    from tasks import setup
    session, git_gecko, git_wpt, gh_wpt, bz = setup()
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
