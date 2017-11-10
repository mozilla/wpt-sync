import re
import sys
import time
import traceback

import git
from mozautomation import commitparser

import base
import log
import settings
import commit as sync_commit
from gitutils import update_repositories
from pipeline import AbortError
from env import Environment

env = Environment()

logger = log.get_logger("upstream")


class BackoutCommitFilter(base.CommitFilter):
    def __init__(self, bug_id):
        self.bug = bug_id
        self.seen = set()

    def filter_commit(self, commit):
        if commit.bug == self.bug_id:
            self.seen.add(commit.canonical_rev)
            return True
        elif commit.is_backout:
            commits, bugs = commit.wpt_commits_backed_out()
            for commit in commits:
                if commit.sha1 in self.seen:
                    return True
        return False

    def filter_commits(self, commits):
        return remove_complete_backouts(commits)


class WptUpstreamSync(base.WptSyncProcess):
    sync_type = "upstream"
    obj_id = "bug"
    statuses = ("open", "merged", "landed")

    @classmethod
    def load(cls, git_gecko, git_wpt, branch_name):
        sync = base.WptSyncProcess.load(cls, git_gecko, git_wpt, branch_name)
        sync.update_wpt_refs()
        return sync

    @classmethod
    def for_bug(cls, git_gecko, git_wpt, bug):
        syncs = cls.load_all(git_gecko, git_wpt, status="*", obj_id=bug)
        if len(syncs) == 0:
            return None

        if len(syncs) > 1:
            for status in ["open", "merged"]:
                status_syncs = [item for item in syncs if item.status == status]
                if status_syncs:
                    assert len(status_syncs) == 1
                syncs = status_syncs
                break
        return syncs[0]

    def gecko_commit_filter(self):
        return BackoutCommitFilter(self.bug)

    def update_wpt_refs(self):
        # Check if the remote was updated under us
        remote_branch, _ = self.remote_branch(create=False)
        if remote_branch is None:
            return

        remote_head = self.git_wpt.refs("origin/%s" % remote_branch)
        if remote_head.commit.hexsha != self.wpt_commits.head.sha1:
            self.wpt_commits.head = sync_commit.WptCommit(self.git_wpt,
                                                          remote_head.commit.hexsha)

        remote_base = self.git_wpt.merge_base("origin/master", remote_head)
        if remote_base.hexsha != self.wpt_commits.base.sha1:
            self.wpt_commits.base = sync_commit.WptCommit(self.git_wpt, remote_base.hexsha)

    @classmethod
    def last_sync_point(cls, git_gecko, repository_name):
        name = base.SyncPointName(cls.sync_type,
                                  repository_name)
        return base.BranchRefObject(git_gecko, name,
                                    sync_commit.GeckoCommit)

    @property
    def bug(self):
        return self._process_name.obj_id

    def remote_branch(self, create=False):
        local_branch = self.branch_name
        remote = self.git_wpt.rev_parse("%s@{upstream}" % local_branch,
                                        abbrev_ref=True,
                                        with_exceptions=False)
        if remote:
            return remote, True

        if not create:
            return None, False

        count = 0
        remote_refs = self.git_wpt.remotes("origin").refs
        name = local_branch
        while name in remote_refs:
            count += 1
            name = "%s_%s" % (local_branch, count)

        return name, False

    def upstreamed_gecko_commits(self):
        if self._upstreamed_gecko_commits is None:
            self._upstreamed_gecko_commits = [
                sync_commit.GeckoCommit(self.git_gecko,
                                        self.git_gecko.cinnbar.hg2git(
                                            wpt_commit.metadata["gecko-commit"]))
                for wpt_commit in self.wpt_commits]
        return self._upstreamed_gecko_commits

    def update_wpt_commits(self):
        for i, (gecko_commit, upstream_commit) in enumerate(
                zip(self.gecko_commits, self.upstreamed_gecko_commits)):
            if upstream_commit.metadata.get("gecko-commit") != gecko_commit.canonical_rev:
                break

        if i + 1 == len(self.gecko_commits) == len(self.upstreamed_gecko_commits):
            return False

        if i < len(self.upstreamed_gecko_commits) - 1:
            self.wpt_commits.head = self.upstreamed_gecko_commits[i]

        for commit in self.gecko_commits[i:]:
            self.add_commit(commit)

        return True

    @property
    def pr(self):
        return self.data["pr"]

    @pr.setter
    def pr(self, value):
        self.data["pr"] = value

    def gecko_landed(self):
        if not len(self.gecko_commits):
            return False
        landed = [self.git_gecko.is_ancestor(commit.sha1, env.config["gecko"]["refs"]["central"])
                  for commit in self.gecko_commits]
        if not all(item == landed[0] for item in landed):
            raise ValueError("Got some commits landed and some not for upstream sync %s" %
                             self.branch)
        return landed[0]

    def commits_missing(self):
        try:
            self.git_gecko.rev_parse(commit.metadata["gecko-commit"])
        except git.exc.BadName:
            return True
        return False

    def add_commit(self, gecko_commit):
        git_work = self.wpt_worktree.get()

        metadata = {"gecko-commit": gecko_commit.canonical_rev,
                    "gecko-integration-branch": self.repository.name}

        wpt_commit = gecko_commit.move(git_work,
                                       metadata=metadata,
                                       msg_filter=commit_message_filter,
                                       src_prefix=env.config["gecko"]["path"]["wpt"])

        return wpt_commit, True

    def create_pr(self):
        remote_branch, _ = self.remote_branch()
        while not env.gh_wpt.get_branch(remote_branch):
            logger.debug("Waiting for branch")
            time.sleep(1)

        summary, body = self.gecko_commits[0].commit.message.split("\n", 1)
        remote_branch, _ = self.remote_branch()
        pr_id = env.gh_wpt.create_pull(
            title="[Gecko%s] %s" % (" bug %s" % self.bug if self.bug else "", summary),
            body=body.strip(),
            base="master",
            head=remote_branch)
        env.gh_wpt.approve_pull(self.pr_id)
        self.pr = pr_id
        # TODO: add label to bug
        env.bz.comment(self.bug,
                        "Created web-platform-tests PR %s for changes under testing/web-platform/tests" %
                        env.gh_wpt.pr_url(pr_id))
        return pr_id

    def push_commits(self):
        remote_branch, _ = self.remote_branch(create=True)
        logger.info("Pushing commits from bug %s to branch %s" % (self.bug, remote_branch))
        self.git_wpt.remotes["origin"].push("%s:%s" % (self.branch_name, remote_branch),
                                            force=True,
                                            set_upstream=True)
        landed_status = "success" if self.gecko_landed() else "failure"
        # TODO - Maybe ignore errors setting the status
        env.gh_wpt.set_status(self.pr,
                              landed_status,
                              target_url=env.bz.bug_url(self.bug),
                              description="Landed on mozilla-central",
                              context="upstream/gecko")

    def update_github(self):
        if self.pr:
            if not len(self.wpt_commits()):
                env.gh_wpt.close_pull(self.pr)
            elif env.gh_wpt.pull_state(self.pr) == "closed":
                env.gh_wpt.reopen_pull(self.pr)
        self.push_commits()
        if not self.pr:
            self.create_pr()

    def is_mergeable(self):
        return (self.gecko_landed() and
                env.gh_wpt.status_checks_pass(self.pr) and
                env.gh_wpt.is_mergeable(self.pr_id))

    def try_land_pr(self):
        if not self.gecko_landed():
            return False

        if not self.pr:
            return False

        logger.info("Trying to land %s" % self.pr)

        msg = None
        if not env.gh_wpt.status_checks_pass(self.pr):
            # TODO: get some details in this message
            msg = "Can't merge web-platform-tests PR due to failing upstream tests"
        elif not env.gh_wpt.is_mergeable(self.pr):
            msg = "Can't merge web-platform-tests PR because it has merge conflicts"
        else:
            # First try to rebase the PR
            try:
                self.rebase()
            except AbortError:
                msg = "Rebasing web-platform-tests PR branch onto origin/master failed"

            try:
                env.gh_wpt.merge_pull(self.pr)
            except Exception as e:
                # TODO: better exception type
                msg = "Merging PR failed: %s" % e
            else:
                # Now close the sync by marking the branch as merged
                self.status = "merged"
                return True
        logger.error(msg)
        env.bz.comment(self.bug, msg)
        return False


def commit_message_filter(msg):
    metadata = {}
    m = commitparser.BUG_RE.match(msg)
    if m:
        bug_str = m.group(0)
        if msg.startswith(bug_str):
            # Strip the bug prefix
            prefix = re.compile("^%s[^\w\d]*" % bug_str)
            msg = prefix.sub("", msg)
        metadata["bugzilla-url"] = env.bz.bugzilla_url(bug_str)

    reviewers = ", ".join(commitparser.parse_reviewers(msg))
    if reviewers:
        metadata["gecko-reviewers"] = reviewers
    msg = commitparser.replace_reviewers(msg, "")
    msg = commitparser.strip_commit_metadata(msg)

    return msg, metadata


def wpt_commits(git_gecko, first_commit, head_commit):
    # List of syncs that have changed, so we can update them all as appropriate at the end
    revish = "%s..%s" % (first_commit.sha1, head_commit.sha1)
    return [sync_commit.GeckoCommit(git_gecko, item.hexsha) for item in
            git_gecko.iter_commits(revish,
                                   paths=env.config["gecko"]["path"]["wpt"],
                                   reverse=True)]


def remove_complete_backouts(commits):
    """Given a list of commits, remove any commits for which a backout exists
    in the list"""
    commits_remaining = set()
    for commit in commits:
        if commit.is_backout:
            backed_out = set(item.sha1 for item, _ in commit.wpt_commits_backed_out())
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
        if self._second:
            return self._second.sha1
        else:
            self._first.sha1

    @head.setter
    def head(self, value):
        self._second = value


def updates_for_backout(syncs_by_bug, commit):
    backout_commit_shas, bugs = commit.wpt_commits_backed_out()
    backout_commit_shas = set(backout_commit_shas)

    create_syncs = {}
    update_syncs = {}

    # Check each sync for the backed-out commits
    for target_sync in sorted(syncs_by_bug.values(),
                              key=lambda x: 0 if x.bug in bugs else 1):
        gecko_shas = set(item.canonical_rev for item in target_sync.upstream_gecko_commits)
        for backout_commit in backout_commit_shas[:]:
            if backout_commit in gecko_shas:
                update_syncs[target_sync.bug] = backout_commit
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
                                 "web-platform-tests")
            sync = WptUpstreamSync.new(git_gecko,
                                       git_wpt,
                                       bug=bug,
                                       gecko_base=endpoint.base,
                                       gecko_head=endpoint.head)
            rv.append(sync)
    return rv


def update_sync_heads(syncs_by_bug, heads_by_bug):
    rv = []
    for bug, commit in heads_by_bug.iteritems():
        sync = syncs_by_bug[bug]
        sync.gecko_commits.head = commit
        rv.append(sync)
    return rv


# Entry point
def push(git_gecko, git_wpt, repository_name, rev):
    update_repositories(git_gecko, git_wpt, repository_name)

    pushed_syncs = set()
    landed_syncs = set()
    failed_syncs = set()

    last_sync_point = WptUpstreamSync.last_sync_point(git_gecko, repository_name)
    if last_sync_point.commit is None:
        # If we are just starting, default to the current HEAD
        logger.info("No existing sync point for %s found, using the latest HEAD")
        last_sync_point.create(rev)

    wpt_syncs = WptUpstreamSync.load_all(git_gecko, git_wpt)

    syncs_by_bug = {item.bug: item for item in wpt_syncs}

    create_endpoints, update_syncs = updated_syncs_for_push(git_gecko,
                                                            git_wpt,
                                                            repository_name,
                                                            last_sync_point,
                                                            rev,
                                                            syncs_by_bug)

    to_push = create_syncs(create_endpoints)
    to_push.extend(update_sync_heads(syncs_by_bug, update_syncs))

    for sync in to_push:
        updated = sync.update_wpt_commits()
        if updated:
            try:
                sync.update_github()
            except Exception:
                failed_syncs.add(sync)
            else:
                pushed_syncs.add(sync)

    # TODO: check this name
    if git_wpt.is_ancestor(rev, env.config["gecko"]["refs"]["central"]):
        for sync in wpt_syncs:
            if sync not in failed_syncs:
                if sync.try_land_pr():
                    landed_syncs.add(sync)

    last_sync_point = rev

    return pushed_syncs, landed_syncs, failed_syncs


# Entry point
def status_changed(git_gecko, sync, context, status, url):
    landed = False
    if status == "pending":
        # Never change anything for pending
        return landed

    if status == "passed":
        if sync.gecko_landed():
            landed = sync.try_land_pr()
        else:
            env.bz.comment(sync.bug, "Upstream web-platform-tests status checked passed, "
                           "PR will merge once commit reaches central.")
    else:
        env.bz.comment(sync.bug, "Upstream web-platform-tests status %s for %s. "
                       "This will block the upstream PR from merging. "
                       "See %s for more information" % (status, context, url))
    return landed


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
