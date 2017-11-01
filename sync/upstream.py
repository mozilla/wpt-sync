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
from mozautomation import commitparser
from sqlalchemy.orm import joinedload

import base
import log
import model
import settings
import commit as sync_commit
from gitutils import is_ancestor, pr_for_commit
from pipeline import pipeline, step, AbortError, MultipleExceptions
from worktree import ensure_worktree, remove_worktrees

logger = log.get_logger("upstream")


class WptUpstreamSync(base.WptSyncProcess):
    branch_prefix = "gecko_upstream_sync"

    statues = ("open", "merged", "landed")

    @staticmethod
    def has_metadata(commit):
        bug_url = commit.metadata.get("bugzilla-url")
        if not bug_url:
            return False
        if not is_bugzilla_url(bug_url):
            return False
        if "gecko-commit" not in commit.metadata:
            return False
        return True

    @classmethod
    def for_branch(cls, git_gecko, git_wpt, branch):
        data = cls.parse_branch(branch)
        assert data is not None
        bug, status = data

        return cls(git_gecko, git_wpt, bug, status)

    @classmethod
    def for_bug(cls, git_gecko, git_wpt, bug):
        branch_prefix = "_".join(self.branch_prefix, bug)
        for branch in git_gecko.branches:
            if branch.name.startswith(branch_prefix):
                # TODO: Prefer open bugs if there are more than one
                return cls.for_branch(git_gecko, git_gecko, branch.name)

    def remote_branch(self):
        local_branch = self.branch_name
        remote = self.git_wpt.rev_parse("%s@{upstream}" % local_branch,
                                        abbrev_ref=True,
                                        with_exceptions=False)
        if remote:
            return remote, True

        count = 0
        remote_refs = self.git_wpt.remotes("origin").refs
        name = local_branch
        while name in remote_refs:
            count += 1
            name = "%s_%s" % (local_branch, count)

        return name, False

    @property
    def pr(self):
        pr_for_commit(self.git_wpt, self.branch)

    def wpt_commits(self):
        if self._wpt_commits is None:
            wpt_commits = []
            for item in self.git_wpt.itercommits("origin/master..%s" % self.branch):
                wpt_commit = sync_commit.GeckoCommit(self.git_wpt, item.hexsha)

                if "gecko-commit" not in wpt_commit.metadata:
                    raise ValueError(
                        "web-platform-tests commit %s looks like it upstreams from bug %s, "
                        "but there is no associated gecko-commit metadata" %
                        (wpt_commit.hexsha, self.bug))
                if not wpt_commit.metadata.get("bugzilla-url", "").endswith("=%s" % self.bug):
                    raise ValueError(
                        "web-platform-tests commit %s looks like it upstreams from bug %s, "
                        "but the bugzilla-url metadata is invalid" %
                        (wpt_commit.hexsha, self.bug))
                wpt_commits.append(wpt_commit)
            self._wpt_commits = wpt_commits
        return self._wpt_commits

    def gecko_commits(self):
        if self._gecko_commits is None:
            self._gecko_commits = [
                sync_commit.Commit(self.git_gecko, wpt_commit.metadata["gecko-commit"])
                for wpt_commit in self.wpt_commits]
        return self._gecko_commits

    def gecko_landed(self):
        gecko_commits = self.gecko_commits()
        if not gecko_commits:
            # Not sure this is right
            return False
        landed = [self.git_gecko.is_ancestor(commit.sha, config["gecko"]["refs"]["central"])
                  for commit in gecko_commits]
        if not all(item == landed[0] for item in landed):
            raise ValueError("Got some commits landed and some not for upstream sync %s" %
                             self.branch)
        return landed[0]

    def commits_missing(self):
        """Check if any gecko commits have disappeared due to history rewriting"""
        for commit in self.wpt_commits:
            try:
                self.git_gecko.rev_parse(commit.metadata["gecko-commit"])
            except git.exc.BadName:
                return True
        return False

    def reset(self, target="origin/master"):
        # This will leave the working-tree if any in a dirty state
        self.git_wpt.git.update_ref("refs/heads/%s" % self.branch, target)
        self._wpt_commits = None
        self._gecko_commits = None

    def add_commit(self, gecko_commit):
        for item in self.gecko_commits:
            if item.sha1 == gecko_commit.sha1:
                return item, False

        metadata = {"gecko-commit": gecko_commit.canonical_rev,
                    "gecko-integration-branch": self.repository.name}

        wpt_commit = c.move(git_work,
                            metadata=metadata,
                            msg_filter=commit_message_filter,
                            src_prefix=config["gecko"]["path"]["wpt"])

        self._gecko_commits.append(gecko_commit)
        self._wpt_commits.append(wpt_commit)

        return wpt_commit, True

    def remove_commits(self, backed_out):
        if len(backed_out) == 0:
            return

        backed_out_sha1s = set(item.sha1 for item in backed_out)

        remove_commits = [(item, item.metadata["gecko-commit"] in backed_out_sha1s)
                          for commit in self.wpt_commits]
        tail_count = 0
        for commit, remove in reversed(remove_commits):
            if remove:
                tail_count += 1
            else:
                break
        if tail_count == len(backed_out):
            # All the removals form a block at the end of the commits
            # in this case we don't need to do anything in a working tree but can just
            # reset the branch to the remaining commits
            keep_commits = remove_commits[:-tail_count]
            assert all(remove is False for _, remove in keep_commits)
            new_ref = keep_commits[-1].sha if keep_commits else "origin/master"
            self.reset(new_ref)
        else:
            logger.warning("Bug %s has a non-simple backout")
            self.reset()
            git_work_wpt = self.wpt_worktree()
            git_work_wpt.head.reset(working_tree=True)
            for commit, remove in keep_commits:
                if not remove:
                    git_work.git.cherry_pick(commit.sha)

    def create_pr(self):
        remote_branch, _ = self.remote_branch()
        while not self.gh_wpt.get_branch(remote_branch):
            logger.debug("Waiting for branch")
            time.sleep(1)

        summary, body = self.gecko_commits[0].commit.message.split("\n", 1)
        remote_branch, _ = self.remote_branch()
        pr_id = self.gh_wpt.create_pull(title="[Gecko%s] %s" % (" bug %s" % self.bug if self.bug else "", summary),
                                        body=body.strip(),
                                        base="master",
                                        head=remote_branch)
        self.gh_wpt.approve_pull(self.pr_id)
        # TODO: add label to bug
        self.bz.comment(sync.bug,
                        "Created web-platform-tests PR %s for changes under testing/web-platform/tests" %
                        pr_url(config, pr_id))

        return pr_id

    def push_commits(self):
        remote_branch, _ = self.remote_branch()
        logger.info("Pushing commits from bug %s to branch %s" % (sync.bug, sync.wpt_branch))
        git_wpt.remotes["origin"].push("%s:%s" % (self.branch_name, remote_branch), force=True,
                                       set_upstream=True)
        landed_status = "success" if self.gecko_landed() else "failure"
        # TODO - Maybe ignore errors setting the status
        self.gh_wpt.set_status(self.pr,
                               status,
                               target_url=bugzilla_url(self.bug),
                               description="Landed on mozilla-central",
                               context="upstream/gecko")

    def rebase(self):
        wpt_work = self.wpt_worktree()
        try:
            wpt_work.git.rebase("origin/master")
        except git.GitCommandError as e:
            raise AbortError("Rebasing onto master failed:\n%s" % e)

    def push_to_github(self):
        self.push_commits()
        if not self.pr:
            pr_id = self.create_pr()

    def is_mergeable(self):
        return (self.gecko_landed() and
                self.gh_wpt.status_checks_pass(self.pr) and
                self.gh_wpt.is_mergeable(sync.pr_id))

    def try_land_pr(self):
        if not self.gecko_landed():
            return False

        if not self.pr:
            return False

        logger.info("Trying to land %s" % self.pr)

        msg = None
        if not self.gh_wpt.status_checks_pass(self.pr):
            # TODO: get some details in this message
            msg = "Can't merge web-platform-tests PR due to failing upstream tests"
        elif not self.gh_wpt.is_mergeable(self.pr):
            msg = "Can't merge web-platform-tests PR because it has merge conflicts"
        else:
            # First try to rebase the PR
            try:
                self.rebase()
            except AbortError:
                msg = "Rebasing web-platform-tests PR branch onto origin/master failed"

            try:
                self.gh_wpt.merge_pull(self.pr)
            except Exception as e:
                # TODO: better exception type
                msg = "Merging PR failed: %s" % e
            else:
                # Now close the sync by marking the branch as merged
                self.status = "merged"
                return True
        logger.error(msg)
        self.bz.comment(self.bug, msg)
        return False

    def is_empty(self):
        return len(self.wpt_commits) == 0

    def close(self):
        self.gh_wpt.close_pull(self.pr_id)


bugzilla_prefix = "https://bugzilla.mozilla.org/show_bug.cgi?id="


def bugzilla_url(bug):
    return "%s%s" % (bugzilla_prefix, bug)


def is_bugzilla_url(url):
    if not url.startswith(bugzilla_prefix):
        return False
    try:
        int(url[len(bugzilla_prefix):])
    except ValueError:
        return False
    return True


def load_syncs(git_gecko, git_wpt, status="open"):
    rv = []
    for branch in git_wpt.branches:
        data = WptUpstreamSync.parse_branch(branch.name)
        # Check the status is open
        if data:
            if status and data[1] == status:
                rv.append(WptUpstreamSync.for_branch(git_gecko, git_wpt, branch.name))
    return rv


def commit_message_filter(msg):
    metadata = {}
    m = commitparser.BUG_RE.match(msg)
    if m:
        bug_str = m.group(0)
        if msg.startswith(bug_str):
            # Strip the bug prefix
            prefix = re.compile("^%s[^\w\d]*" % bug_str)
            msg = prefix.sub("", msg)
        metadata["bugzilla-url"] = bugzilla_url(bug_str)

    reviewers = ", ".join(commitparser.parse_reviewers(msg))
    if reviewers:
        metadata["gecko-reviewers"] = reviewers
    msg = commitparser.replace_reviewers(msg, "")
    msg = commitparser.strip_commit_metadata(msg)

    return msg, metadata


def wpt_commits(config, git_gecko, revish):
    gecko_wpt_path = config["gecko"]["path"]["wpt"]

    all_commits = list(git_gecko.iter_commits(revish))

    wpt_commits = [commit_wpt.GeckoCommit(git_gecko, item.hexsha) for item in
                   git_gecko.iter_commits(revish, paths=gecko_wpt_path)]

    return wpt_commits


def updated_commits(config, git_gecko, first_commit, head_commit, repository_name):
    # List of syncs that have changed, so we can update them all as appropriate at the end
    head_commit = head_commit if head_commit else config["gecko"]["refs"][repository_name]
    revish = "%s..%s" % (first_commit, head_commit) if first_commit else head_commit

    return wpt_commits(config, git_gecko, revish)


def syncs_for_push(config, git_gecko, git_wpt, bz, repository_name, first_commit, head_commit,
                   syncs_by_bug):
    commits = updated_commits(config, git_gecko, first_commit, head_commit, repository_name)
    if not commits:
        logger.info("No new commits affecting wpt found")
        return

    # Remove backout pairs as an optimisation
    commits_remaining = set()
    for commit in commits:
        if commit.is_backout:
            backed_out = set(item.sha1 for item, _ in commit.wpt_commits_backed_out())
            if backed_out.issubset(commits_remaining):
                commits_remaining -= backed_out
                continue
        commits_remaining.add(commit.sha1)

    commits = [item for item in commits if item.sha1 in commits_remaining]

    if not commits:
        logger.info("No commits remain after removing backout pairs")
        return

    updated_syncs = set()
    failed_syncs = set()

    for commit in commits:
        if commit.is_backout:
            backout_commit_shas, bugs = commit.wpt_commits_backed_out()
            backout_commit_shas = set(backout_commit_shas)
            backed_out_by_sync = defaultdict(list)
            # Check each sync for the backed-out commits
            for target_sync in sorted(syncs_by_bug.values(),
                                      key=lambda x: 0 if x.bug in bugs else 1):
                gecko_shas = set(item.sha1 for item in target_sync.gecko_commits)
                for backout_commit in backout_commit_shas[:]:
                    if backout_commit in gecko_shas:
                        backed_out_by_sync[target_sync.bug].append(
                            wpt_commit.GeckoCommit(git_gecko, backout_commit))
                        backout_commit_shas.remove(backout_commit)
                if not backout_commit_shas:
                    break

            if backout_commit_shas:
                # This backout covers something other than known open syncs, so we need to
                # create a new sync especially for it
                # TODO: we should check for this already existing before we process the backout
                # Need to create a bug for this backout
                backout_bug = None
                for bug in bugs:
                    if bug not in syncs_by_bug:
                        backout_bug = bug
                        break
                if backout_bug is None:
                    backout_bug = bz.new(
                        "Tracking bug for upstreaming backout of %s" % commit.canonical_rev,
                        "",
                        "Testing",
                        "web-platform-tests")
                sync = WptUpstreamSync(git_gecko, git_wpt, backout_bug)
                try:
                    sync.add_commit(commit)
                except Exception as e:
                        failed_syncs.add(sync)
                        logger.error(e)
                else:
                    updated_syncs.add(sync)
            else:
                for sync, backed_out_commits in backed_out_by_sync.iteritems():
                    if sync in failed_syncs:
                        continue
                    try:
                        sync.remove_commits(backed_out_commits)
                    except Exception as e:
                        failed_syncs.add(sync)
                        logger.error(e)
                        bz.comment(sync.bug,
                                   "Upstreaming to web-platform-tests failed because applying "
                                   "backout from rev %s failed:\n%s" % (commit.canonical_rev, e))
                        updated_syncs.add(sync)
        else:
            bug = commit.bug
            if bug in syncs_by_bug:
                sync = syncs_by_bug[bug]
            else:
                sync = WptUpstreamSync(git_gecko, git_wpt, bug)
                syncs_by_bug[bug] = sync

            if sync in failed_syncs:
                continue
            try:
                sync.add_commit(commit)
            except Exception as e:
                failed_syncs.add(sync)

                logger.error(e)
                bz.comment(sync.bug,
                           "Upstreaming to web-platform-tests failed because creating patch "
                           "from rev %s failed:\n%s" % (c.canonical_rev, e))
            else:
                updated_syncs.add(sync)

    return updated_syncs, failed_syncs


def update_repositories(git_gecko, git_wpt, repository_name):
    logger.info("Fetching mozilla-unified")
    git_gecko.remotes.mozilla.fetch()
    logger.info("Fetch done")

    if repository_name == "autoland":
        logger.info("Fetch autoland")
        git_gecko.remotes.autoland.fetch()
        logger.info("Fetch done")

    git_wpt.git.fetch("origin")


def get_sync_commit(config, git_gecko, repository_name, rev):
    try:
        last_sync_commit = git_gecko.tags["wpt_sync_point_%s" % repository_name].commit.hexsha
    except IndexError:
        last_sync_commit = None

    if not last_sync_commit:
        # If we are just starting, default to the current HEAD
        logger.info("No existing sync point for %s found, using the latest HEAD")
        return rev
    return last_sync_commit


def push(config, session, git_gecko, git_wpt, gh_wpt, bz, repository_name, rev):
    WptUpstreamSync.config = config
    WptUpstreamSync.gh_wpt = gh_wpt
    WptUpstreamSync.bz = bz

    update_repositories(git_gecko, git_wpt, repository_name)
    last_sync_point = get_sync_commit(config, git_gecko, repository_name, rev)
    wpt_syncs = load_syncs(git_gecko, git_wpt)
    syncs_by_bug = {item.bug: item for item in wpt_syncs}

    updated_syncs, failed_syncs = syncs_for_push(config, git_gecko, git_wpt, bz,
                                                 repository_name, last_sync_point,
                                                 rev, syncs_by_bug)

    for sync in updated_syncs:
        try:
            sync.push_to_github()
        except Exception:
            failed_syncs.add(sync)

    if repository_name == config["sync"]["landing"]:
        for sync in syncs:
            if sync not in failed_syncs:
                sync.try_land_pr()
    git_gecko.create_tag("wpt_sync_point_%s" % repository_name, )
    return updated_syncs, failed_syncs


def status_changed(git_gecko, sync, context, status, url):
    if status == "pending":
        # Never change anything for pending
        return

    if status == "passed":
        if sync.gecko_landed():
            sync.try_land_pr()
        else:
            bz.comment(sync.bug, "Upstream web-platform-tests status checked passed, "
                       "PR will merge once commit reaches central.")
    else:
        bz.comment(sync.bug, "Upstream web-platform-tests status %s for %s. "
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
