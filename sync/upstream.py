import os
import re
import time
import traceback

import git
from mozautomation import commitparser

import base
import log
import commit as sync_commit
from gitutils import update_repositories, gecko_repo
from pipeline import AbortError
from env import Environment

env = Environment()

logger = log.get_logger(__name__)


class BackoutCommitFilter(base.CommitFilter):
    def __init__(self, bug_id):
        self.bug = bug_id
        self.seen = set()

    def filter_commit(self, commit):
        if commit.is_empty(env.config["gecko"]["path"]["wpt"]):
            return False
        if commit.is_backout:
            commits, bugs = commit.wpt_commits_backed_out()
            for backout_commit in commits:
                if backout_commit in self.seen:
                    return True
        if commit.bug == self.bug:
            self.seen.add(commit.canonical_rev)
            return True
        return False

    def filter_commits(self, commits):
        return remove_complete_backouts(commits)


class UpstreamSync(base.SyncProcess):
    sync_type = "upstream"
    obj_id = "bug"
    statuses = ("open", "complete", "incomplete")

    def __init__(self, *args, **kwargs):
        super(UpstreamSync, self).__init__(*args, **kwargs)

        self._upstreamed_gecko_commits = None
        self._upstreamed_gecko_head = None

    @classmethod
    def load(cls, git_gecko, git_wpt, branch_name):
        sync = super(UpstreamSync, cls).load(git_gecko, git_wpt, branch_name)
        sync.update_wpt_refs()
        return sync

    @classmethod
    def for_bug(cls, git_gecko, git_wpt, bug):
        for status in cls.statuses:
            syncs = cls.load_all(git_gecko, git_wpt, status=status, obj_id=bug)
            assert len(syncs) <= 1
            if len(syncs) == 1:
                return syncs[0]

    @classmethod
    def for_pr(cls, git_gecko, git_wpt, pr_id):
        try:
            wpt_head = git_wpt.commit("origin/pr/%s" % pr_id).hexsha
        except git.BadName:
            return None
        for status in ["open", "complete", "incomplete"]:
            syncs = cls.load_all(git_gecko, git_wpt, status=status, obj_id="*")

            for sync in syncs:
                if sync.pr == pr_id:
                    return sync
                elif sync.pr is None and sync.wpt_commits.head.sha1 == wpt_head:
                    # This is to handle cases where the pr was not correctly stored
                    sync.pr = pr_id
                    return sync

    @classmethod
    def from_pr(cls, git_gecko, git_wpt, pr_id, body):
        gecko_commits = []
        bug = None
        integration_branch = None

        if not cls.has_metadata(body):
            return None

        commits = env.gh_wpt.get_commits(pr_id)

        for gh_commit in commits:
            commit = sync_commit.WptCommit(git_wpt, gh_commit.sha)
            if cls.has_metadata(commit.message):
                gecko_commits.append(git_gecko.cinnabar.hg2git(commit.metadata["gecko-commit"]))
                commit_bug = env.bz.id_from_url(commit.metadata["bugzilla-url"])
                if bug is not None and commit_bug != bug:
                    logger.error("Got multiple bug numbers in URL from commits")
                    break
                elif bug is None:
                    bug = commit_bug

                if (integration_branch is not None and
                    commit.metadata["integration_branch"] != integration_branch):
                    logger.warning("Got multiple integration branches from commits")
                elif integration_branch is None:
                    integration_branch = commit.metadata["integration_branch"]
            else:
                break

        if not gecko_commits:
            return None

        assert bug
        gecko_base = git_gecko.rev_parse("%s^" % gecko_commits[0])
        gecko_head = git_gecko.rev_parse(gecko_commits[-1])
        wpt_head = commits[-1].sha
        wpt_base = commits[0].sha

        return cls.new(git_gecko, git_wpt, gecko_base, gecko_head,
                       wpt_base, wpt_head, bug. pr_id)

    @classmethod
    def has_metadata(cls, message):
        required_keys = ["gecko-commit",
                         "gecko-integration-branch",
                         "bugzilla-url"]
        metadata = sync_commit.get_metadata(message)
        return all(item in metadata for item in required_keys)

    def update_status(self, action, merge_sha=None, base_sha=None):
        """Update the sync status for a PR event on github

        :param action string: Either a PR action or a PR status
        :param merge_sha boolean: SHA of the new head if the PR merged or None if it didn't
        """
        if action == "closed":
            if not merge_sha:
                env.bz.comment(self.bug, "Upstream PR was closed without merging")
                self.pr_status = "closed"
            else:
                self.merge_sha = merge_sha
                env.bz.comment(self.bug, "Upstream PR merged")
        elif action == "reopened" or action == "open":
            self.pr_status = "open"

    def gecko_commit_filter(self):
        return BackoutCommitFilter(self.bug)

    def update_wpt_refs(self):
        # Check if the remote was updated under us
        if self.remote_branch is None:
            return

        remote_head = self.git_wpt.refs["origin/%s" % self.remote_branch]
        if remote_head.commit.hexsha != self.wpt_commits.head.sha1:
            self.wpt_commits.head = sync_commit.WptCommit(self.git_wpt,
                                                          remote_head.commit.hexsha)

    @classmethod
    def last_sync_point(cls, git_gecko, repository_name):
        assert "/" not in repository_name
        name = base.SyncPointName(cls.sync_type,
                                  repository_name)
        return base.BranchRefObject(git_gecko,
                                    name,
                                    commit_cls=sync_commit.GeckoCommit)

    @property
    def bug(self):
        return self._process_name.obj_id

    @property
    def pr(self):
        return self.data.get("pr")

    @pr.setter
    def pr(self, value):
        self.data["pr"] = value

    @property
    def pr_status(self):
        return self.data.get("pr-status", "open")

    @pr_status.setter
    def pr_status(self, value):
        self.data["pr-status"] = value

    @property
    def merge_sha(self):
        return self.data.get("merge-sha", None)

    @merge_sha.setter
    def merge_sha(self, value):
        self.data["merge-sha"] = value

    @property
    def remote_branch(self):
        remote = self.data.get("remote-branch")
        if remote is None:
            remote = self.git_wpt.git.rev_parse("%s@{upstream}" % self.branch_name,
                                                abbrev_ref=True,
                                                with_exceptions=False)
            if remote:
                remote = remote[len("origin/"):]
                self.remote_branch = remote
            else:
                # We get the empty string back, but want to always use None
                # to indicate a missing remote
                remote = None
        return remote

    @remote_branch.setter
    def remote_branch(self, value):
        self.data["remote-branch"] = value

    def get_or_create_remote_branch(self):
        if not self.remote_branch:
            count = 0
            remote_refs = self.git_wpt.remotes.origin.refs
            name = "gecko/%s" % self.bug
            while name in remote_refs:
                count += 1
                name = "gecko/%s/%s" % (self.bug, count)
            self.remote_branch = name
        return self.remote_branch

    @property
    def upstreamed_gecko_commits(self):
        if (self._upstreamed_gecko_commits is None or
            self._upstreamed_gecko_head != self.wpt_commits.head.sha1):
            self._upstreamed_gecko_commits = [
                sync_commit.GeckoCommit(self.git_gecko,
                                        self.git_gecko.cinnabar.hg2git(
                                            wpt_commit.metadata["gecko-commit"]))
                for wpt_commit in self.wpt_commits]
            self._upstreamed_gecko_head = self.wpt_commits.head.sha1
        return self._upstreamed_gecko_commits

    def update_wpt_commits(self):
        matching_commits = []

        if len(self.gecko_commits) == 0:
            self.wpt_commits.head = self.wpt_commits.base

        for gecko_commit, upstream_commit in zip(self.gecko_commits, self.upstreamed_gecko_commits):
            if upstream_commit != gecko_commit:
                break
            else:
                matching_commits.append(upstream_commit)

        if len(matching_commits) == len(self.gecko_commits) == len(self.upstreamed_gecko_commits):
            return False

        if len(matching_commits) == 0:
            self.wpt_commits.head == self.wpt_commits.base
        elif len(matching_commits) < len(self.upstreamed_gecko_commits):
            self.wpt_commits.head = self.wpt_commits[len(matching_commits) - 1]

        # Ensure the worktree is clean
        wpt_work = self.wpt_worktree.get()
        wpt_work.git.clean(f=True, d=True, x=True)

        for commit in self.gecko_commits[len(matching_commits):]:
            self.add_commit(commit)

        assert (len(self.gecko_commits) ==
                len(self.wpt_commits) ==
                len(self.upstreamed_gecko_commits))

        return True

    def gecko_landed(self):
        if not len(self.gecko_commits):
            return False
        landed = [self.git_gecko.is_ancestor(commit.sha1, env.config["gecko"]["refs"]["central"])
                  for commit in self.gecko_commits]
        if not all(item == landed[0] for item in landed):
            raise ValueError("Got some commits landed and some not for upstream sync %s" %
                             self.branch)
        return landed[0]

    @property
    def repository(self):
        # Need to check central before landing repos
        head = self.gecko_commits.head.sha1
        repo = gecko_repo(self.git_gecko, head)
        if repo is None:
            raise ValueError("Commit %s not part of any repository" % head)
        return repo

    def add_commit(self, gecko_commit):
        git_work = self.wpt_worktree.get()

        metadata = {"gecko-commit": gecko_commit.canonical_rev,
                    "gecko-integration-branch": self.repository}

        if os.path.exists(os.path.join(git_work.working_dir, gecko_commit.canonical_rev + ".diff")):
            # If there's already a patch file here then don't try to create a new one
            # because we'll presumbaly fail again
            raise AbortError("Skipping due to existing patch")
        wpt_commit = gecko_commit.move(git_work,
                                       metadata=metadata,
                                       msg_filter=commit_message_filter,
                                       src_prefix=env.config["gecko"]["path"]["wpt"])
        self.wpt_commits.head = wpt_commit

        return wpt_commit, True

    def create_pr(self):
        if self.pr:
            return self.pr

        while not env.gh_wpt.get_branch(self.remote_branch):
            logger.debug("Waiting for branch")
            time.sleep(1)

        commit_summary = self.wpt_commits[0].commit.summary

        body = self.wpt_commits[0].msg.split("\n", 1)
        body = body[1] if len(body) != 1 else ""

        pr_id = env.gh_wpt.create_pull(
            title="[Gecko%s] %s" % (" Bug %s" % self.bug if self.bug else "", commit_summary),
            body=body.strip(),
            base="master",
            head=self.remote_branch)
        self.pr = pr_id
        # TODO: add label to bug
        env.bz.comment(self.bug,
                       "Created web-platform-tests PR %s for changes under "
                       "testing/web-platform/tests" %
                       env.gh_wpt.pr_url(pr_id))
        return pr_id

    def push_commits(self):
        remote_branch = self.get_or_create_remote_branch()
        logger.info("Pushing commits from bug %s to branch %s" % (self.bug, remote_branch))
        self.git_wpt.remotes.origin.push("refs/heads/%s:%s" % (self.branch_name, remote_branch),
                                         force=True, set_upstream=True)

    def update_github(self):
        if self.pr:
            if not len(self.wpt_commits):
                env.gh_wpt.close_pull(self.pr)
            elif env.gh_wpt.pull_state(self.pr) == "closed":
                env.gh_wpt.reopen_pull(self.pr)
        self.push_commits()
        if not self.pr:
            self.create_pr()
        landed_status = "success" if self.gecko_landed() else "failure"
        # TODO - Maybe ignore errors setting the status
        env.gh_wpt.set_status(self.pr,
                              landed_status,
                              target_url=env.bz.bugzilla_url(self.bug),
                              description="Landed on mozilla-central",
                              context="upstream/gecko")

    def try_land_pr(self):
        logger.info("Checking if sync for bug %s can land" % self.bug)
        if not self.gecko_landed():
            logger.info("Commits are not yet landed in gecko")
            return False

        if not self.pr:
            logger.info("No upstream PR created")
            return False

        logger.info("Commit are landable; trying to land %s" % self.pr)

        msg = None
        all_status_success, non_success = env.gh_wpt.status_checks_pass(
            self.pr, exclude="upstream/gecko")
        if not all_status_success:
            if not any(item.state == "pending" for item in non_success.itervalues()):
                details = []
                for context, item in non_success.iteritems():
                    if item.state == "pending":
                        continue
                    url_str = " (%s)" % item.target_url if item.target_url else ""
                    details.append("* %s%s" % (item.context, url_str))
                details = "\n".join(details)
                msg = ("Can't merge web-platform-tests PR due to failing upstream checks:\n%s" %
                       details)
        elif not env.gh_wpt.is_mergeable(self.pr):
            msg = "Can't merge web-platform-tests PR because it has merge conflicts"
        else:
            # First try to rebase the PR
            try:
                self.wpt_rebase("origin/master")
            except AbortError:
                msg = "Rebasing web-platform-tests PR branch onto origin/master failed"

            try:
                merge_sha = env.gh_wpt.merge_pull(self.pr)
            except Exception as e:
                # TODO: better exception type
                msg = "Merging PR failed: %s" % e
            else:
                self.merge_sha = merge_sha
                self.finish()
                return True
        if msg is not None:
            logger.error(msg)
            env.bz.comment(self.bug, msg)
        return False


def commit_message_filter(msg):
    metadata = {}
    m = commitparser.BUG_RE.match(msg)
    if m:
        logger.info(m.groups())
        bug_str, bug_number = m.groups()[:2]
        if msg.startswith(bug_str):
            prefix = re.compile(r"^%s[^\w\d]*" % bug_str)
            msg = prefix.sub("", msg)
        metadata["bugzilla-url"] = env.bz.bugzilla_url(bug_number)

    reviewers = ", ".join(commitparser.parse_reviewers(msg))
    if reviewers:
        metadata["gecko-reviewers"] = reviewers
    msg = commitparser.replace_reviewers(msg, "")
    msg = commitparser.strip_commit_metadata(msg)

    return msg, metadata


def wpt_commits(git_gecko, first_commit, head_commit):
    # List of syncs that have changed, so we can update them all as appropriate at the end
    revish = "%s..%s" % (first_commit.sha1, head_commit.sha1)
    logger.info("Getting commits in range %s" % revish)
    commits = [sync_commit.GeckoCommit(git_gecko, item.hexsha) for item in
               git_gecko.iter_commits(revish,
                                      paths=env.config["gecko"]["path"]["wpt"],
                                      reverse=True,
                                      max_parents=1)]
    return [item for item in commits if not item.metadata.get("wptsync-skip")]


def remove_complete_backouts(commits):
    """Given a list of commits, remove any commits for which a backout exists
    in the list"""
    commits_remaining = set()
    for commit in commits:
        if commit.is_backout:
            backed_out, _ = commit.wpt_commits_backed_out()
            backed_out = set(backed_out)
            if backed_out.issubset(commits_remaining):
                commits_remaining -= backed_out
                continue
        commits_remaining.add(commit.sha1)

    return [item for item in commits if item.sha1 in commits_remaining]


class Endpoints(object):
    def __init__(self, first):
        self._first = first
        self._second = None

    @property
    def base(self):
        return self._first.commit.parents[0].hexsha

    @property
    def head(self):
        if self._second is not None:
            return self._second.sha1
        return self._first.sha1

    @head.setter
    def head(self, value):
        self._second = value

    def __repr__(self):
        return "<Endpoints %s:%s>" % (self.base, self.head)


def updates_for_backout(syncs_by_bug, commit):
    backout_commit_shas, bugs = commit.wpt_commits_backed_out()
    backout_commit_shas = set(backout_commit_shas)

    create_syncs = {}
    update_syncs = {}

    # Check each sync for the backed-out commits
    for target_sync in sorted(syncs_by_bug.values(),
                              key=lambda x: 0 if x.bug in bugs else 1):
        for landing_commit in reversed(target_sync.upstreamed_gecko_commits):
            if landing_commit.sha1 in backout_commit_shas.copy():
                # This will set the sync to include the backout commit
                update_syncs[target_sync.bug] = commit.sha1
                backout_commit_shas.remove(landing_commit.sha1)
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
            create_syncs[None].append(Endpoints(commit))
        else:
            create_syncs[backout_bug] = Endpoints(commit)
    return create_syncs, update_syncs


def updated_syncs_for_push(git_gecko, git_wpt, first_commit, head_commit, syncs_by_bug):
    # TODO: Check syncs with pushes that no longer exist on autoland
    commits = wpt_commits(git_gecko, first_commit, head_commit)
    if not commits:
        logger.info("No new commits affecting wpt found")
        return
    else:
        logger.info("Got %i commits since the last sync point" % len(commits))

    commits = remove_complete_backouts(commits)

    if not commits:
        logger.info("No commits remain after removing backout pairs")
        return

    create_syncs = {None: []}
    update_syncs = {}

    for commit in commits:
        if commit.is_backout:
            create, update = updates_for_backout(syncs_by_bug, commit)
            create_syncs.update(create)
            update_syncs.update(update)
        else:
            bug = commit.bug
            if bug in syncs_by_bug:
                update_syncs[bug] = commit
            else:
                if bug is None:
                    create_syncs[None].append(Endpoints(commit))
                elif bug in create_syncs:
                    create_syncs[bug].head = commit
                else:
                    create_syncs[bug] = Endpoints(commit)

    return create_syncs, update_syncs


def create_syncs(git_gecko, git_wpt, create_endpoints):
    rv = []
    for bug, endpoints in create_endpoints.iteritems():
        if bug is not None:
            endpoints = [endpoints]
        for endpoint in endpoints:
            if bug is None:
                # TODO: Loading the commits doesn't work in this case, because we depend on the bug
                bug = env.bz.new("Upstream commit %s to web-platform-tests" %
                                 endpoint.first.canonical_rev,
                                 "",
                                 "Testing",
                                 "web-platform-tests",
                                 whiteboard="[wptsync upstream]")
            sync = UpstreamSync.new(git_gecko,
                                    git_wpt,
                                    bug=bug,
                                    gecko_base=endpoint.base,
                                    gecko_head=endpoint.head,
                                    wpt_base="origin/master",
                                    wpt_head="origin/master")
            rv.append(sync)
    return rv


def update_sync_heads(syncs_by_bug, heads_by_bug):
    rv = []
    for bug, commit in heads_by_bug.iteritems():
        sync = syncs_by_bug[bug]
        sync.gecko_commits.head = commit
        rv.append(sync)
    return rv


def update_modified_sync(sync):
    sync.update_wpt_commits()

    if len(sync.upstreamed_gecko_commits) == 0:
        logger.info("Sync has no commits, so marking as incomplete")
        sync.status = "incomplete"
    else:
        sync.status = "open"

    sync.update_github()


def update_sync_prs(git_gecko, git_wpt, syncs_by_bug, create_endpoints, update_syncs,
                    raise_on_error=False):
    pushed_syncs = set()
    failed_syncs = set()

    to_push = create_syncs(git_gecko, git_wpt, create_endpoints)
    to_push.extend(update_sync_heads(syncs_by_bug, update_syncs))

    for sync in to_push:
        try:
            update_modified_sync(sync)
        except Exception as e:
            sync.error = e
            if raise_on_error:
                raise
            traceback.print_exc()
            logger.error(e)
            failed_syncs.add((sync, e))
        else:
            sync.error = None
            pushed_syncs.add(sync)

    return pushed_syncs, failed_syncs


def try_land_syncs(syncs):
    landed_syncs = set()
    for sync in syncs:
        if sync.try_land_pr():
            landed_syncs.add(sync)
    return landed_syncs


@base.entry_point("upstream")
def update_sync(git_gecko, git_wpt, sync, raise_on_error=True):
    update_repositories(git_gecko, git_wpt, sync.repository == "autoland")
    assert isinstance(sync, UpstreamSync)
    syncs_by_bug = {sync.bug: sync}
    update_syncs = {sync.bug: sync.gecko_commits.head.sha1}
    pushed_syncs, failed_syncs = update_sync_prs(git_gecko,
                                                 git_wpt,
                                                 syncs_by_bug,
                                                 {},
                                                 update_syncs,
                                                 raise_on_error=raise_on_error)

    if sync.repository == "central" and sync not in failed_syncs:
        landed_syncs = try_land_syncs([sync])
    else:
        landed_syncs = set()

    return pushed_syncs, failed_syncs, landed_syncs


@base.entry_point("upstream")
def push(git_gecko, git_wpt, repository_name, hg_rev, raise_on_error=False,
         base_rev=None):
    i = 0
    while True:
        update_repositories(git_gecko, git_wpt, repository_name == "autoland")

        try:
            rev = git_gecko.cinnabar.hg2git(hg_rev)
        except ValueError:
            if i == 4:
                raise
            else:
                i += 1
                time.sleep(1 * (i + 1))
        else:
            break

    last_sync_point = UpstreamSync.last_sync_point(git_gecko, repository_name)
    if last_sync_point.commit is None:
        # If we are just starting, default to the current mozilla central
        logger.info("No existing sync point for %s found, using the latest mozilla-central")
        last_sync_point.commit = env.config["gecko"]["refs"]["central"]
    else:
        logger.info("Last sync point was %s" % last_sync_point.commit.sha1)

    if base_rev is None:
        if git_gecko.is_ancestor(rev, last_sync_point.commit.sha1):
            logger.info("Last sync point moved past commit")
            return
        base_commit = last_sync_point.commit
    else:
        base_commit = sync_commit.GeckoCommit(git_gecko,
                                              git_gecko.cinnabar.hg2git(base_rev))

    wpt_syncs = (UpstreamSync.load_all(git_gecko, git_wpt, status="open") +
                 UpstreamSync.load_all(git_gecko, git_wpt, status="incomplete"))

    syncs_by_bug = {item.bug: item for item in wpt_syncs}

    updated = updated_syncs_for_push(git_gecko,
                                     git_wpt,
                                     base_commit,
                                     sync_commit.GeckoCommit(git_gecko, rev),
                                     syncs_by_bug)

    if updated is None:
        return set(), set(), set()

    create_endpoints, update_syncs = updated

    pushed_syncs, failed_syncs = update_sync_prs(git_gecko, git_wpt, syncs_by_bug,
                                                 create_endpoints, update_syncs,
                                                 raise_on_error=raise_on_error)

    # TODO: check this name
    if git_gecko.is_ancestor(rev, env.config["gecko"]["refs"]["central"]):
        landable_syncs = [item for item in wpt_syncs if item not in failed_syncs]
        landed_syncs = try_land_syncs(landable_syncs)
    else:
        landed_syncs = set()

    if not git_gecko.is_ancestor(rev, last_sync_point.commit.sha1):
        last_sync_point.commit = rev

    return pushed_syncs, landed_syncs, failed_syncs


@base.entry_point("upstream")
def status_changed(git_gecko, git_wpt, sync, context, status, url, sha):
    landed = False
    if status == "pending":
        # Never change anything for pending
        return landed

    if status == "success":
        sync.error = None
        if sync.gecko_landed():
            landed = sync.try_land_pr()
        else:
            env.bz.comment(sync.bug, "Upstream web-platform-tests status checked passed, "
                           "PR will merge once commit reaches central.")
    else:
        env.bz.comment(sync.bug, "Upstream web-platform-tests status %s for %s. "
                       "This will block the upstream PR from merging. "
                       "See %s for more information" % (status, context, url))
        sync.error = "Travis failed"
    return landed
