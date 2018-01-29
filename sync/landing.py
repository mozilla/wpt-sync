import re
import os
import shutil

import git

import base
import commit as sync_commit
import downstream
import log
import tree
import trypush
import upstream
from env import Environment
from gitutils import update_repositories
from projectutil import Mach
from pipeline import AbortError


env = Environment()

logger = log.get_logger(__name__)


class SyncPoint(object):
    def __init__(self, data=None):
        self._items = {}
        if data is not None:
            self._items.update(data)

    def __getitem__(self, key):
        return self._items[key]

    def __setitem__(self, key, value):
        self._items[key] = value

    def load(self, fp):
        with open(fp) as f:
            self.loads(f)

    def loads(self, data):
        for line in data.split("\n"):
            if line:
                key, value = line.split(": ", 1)
                self._items[key] = value

    def dump(self, fp):
        fp.write(self.dumps() + "\n")

    def dumps(self):
        return "\n".join("%s: %s" % (key, value) for key, value in self._items.iteritems())


class LandingSync(base.SyncProcess):
    sync_type = "landing"
    obj_id = "bug"
    statuses = ("open", "complete")

    @classmethod
    def new(cls, git_gecko, git_wpt, wpt_base, wpt_head, bug=None):
        # There is some chance here we create a bug but never create the branch.
        # Probably need something to clean up orphan bugs

        # The gecko branch is a new one based on master
        gecko_base = cls.gecko_integration_branch()
        gecko_head = cls.gecko_integration_branch()

        if bug is None:
            bug = env.bz.new("Update web-platform-tests to %s" % wpt_head,
                             "",
                             "Testing",
                             "web-platform-tests",
                             whiteboard="[wptsync landing]")

        return super(LandingSync, cls).new(git_gecko, git_wpt,
                                           gecko_base,
                                           gecko_head,
                                           wpt_base=wpt_base,
                                           wpt_head=wpt_head,
                                           bug=bug)

    @classmethod
    def for_bug(cls, git_gecko, git_wpt, bug):
        """Get the landing for a specific Gecko bug number"""

        for status in cls.statuses:
            syncs = cls.load_all(git_gecko, git_wpt, status=status, obj_id=bug)
            assert len(syncs) <= 1
            if len(syncs) == 1:
                return syncs[0]

    def check_finished(self):
        return self.git_gecko.is_ancestor(self.gecko_integration_branch(),
                                          self.branch_name)

    def add_pr(self, pr_id, wpt_commits):
        if len(wpt_commits) > 1:
            assert all(item.pr() == pr_id for item in wpt_commits)

        # Assume we can always use the author of the first commit
        author = wpt_commits[0].author

        git_work_wpt = self.wpt_worktree.get()
        git_work_gecko = self.gecko_worktree.get()

        metadata = {
            "wpt-pr": pr_id,
            "wpt-commits": ", ".join(item.sha1 for item in wpt_commits)
        }

        dest_path = os.path.join(git_work_gecko.working_dir,
                                 env.config["gecko"]["path"]["wpt"])

        pr = env.gh_wpt.get_pull(int(pr_id))

        upstream_changed = set()
        for commit in wpt_commits:
            stats = commit.commit.stats
            upstream_changed |= set(stats.files.keys())

        logger.info("Upstream files changed:\n%s" % "\n".join(sorted(upstream_changed)))

        logger.info("Setting wpt HEAD to %s" % wpt_commits[-1].sha1)
        git_work_wpt.head.reference = wpt_commits[-1].commit
        git_work_wpt.head.reset(index=True, working_tree=True)
        shutil.rmtree(dest_path)
        shutil.copytree(git_work_wpt.working_dir, dest_path)

        git_work_gecko.git.add(env.config["gecko"]["path"]["wpt"],
                               no_ignore_removal=True)

        # Some files we don't want to update on import ever
        ignore_paths = ["LICENSE"]
        all_paths = [item for item in git_work_gecko.git.ls_files().split("\n")
                     if item.strip()]
        for rel_path in ignore_paths:
            path = os.path.join(env.config["gecko"]["path"]["wpt"], rel_path)
            if path in all_paths:
                logger.debug("Resetting %s" % path)
                git_work_gecko.index.reset([path])
                git_work_gecko.head.checkout(paths=[path])
            else:
                logger.debug("Path %s is not in the repository" % path)

        message = """Bug %s [wpt PR %s] - %s, a=testonly

Automatic update from web-platform-tests%s
""" % (self.bug, pr.number, pr.title, "\n%s" % pr.body if pr.body else "")
        message = sync_commit.Commit.make_commit_msg(message, metadata)
        commit = git_work_gecko.index.commit(message=message, author=author)
        logger.debug("Gecko files changed: \n%s" % "\n".join(commit.stats.files.keys()))
        gecko_commit = sync_commit.GeckoCommit(self.git_gecko, commit.hexsha)
        self.gecko_commits.head = commit

        return gecko_commit

    def unlanded_gecko_commits(self):
        """Get a list of gecko commit sha1s that correspond to commits which have
        landed on the gecko integration branch, but are not yet merged into the
        upstream commit we are updating to.

        There are two possible sources of such commits:
          * Unlanded PRs. These correspond to upstream syncs with status of "open"
          * Gecko PRs that landed between the wpt commit that we are syncing to
            and latest upstream master.

        :return: List of sha1s in the order in which they originally landed in gecko"""

        commits = []

        def on_integration_branch(commit):
            # Calling this continually is O(N*M) where N is the number of unlanded commits
            # and M is the average depth of the commit in the gecko tree
            # If we need a faster implementation one approach would be to store all the
            # commits not on the integration branch and check if
            return self.git_gecko.is_ancestor(commit.sha1, self.gecko_integration_branch())

        # All the commits from unlanded upstream syncs that are reachable from the
        # integration branch
        unlanded_syncs = set(upstream.UpstreamSync.load_all(self.git_gecko, self.git_wpt,
                                                            status="open"))
        for sync in unlanded_syncs:
            branch_commits = [commit.sha1 for commit in sync.gecko_commits if
                              on_integration_branch(commit)]
            if branch_commits:
                logger.info("Commits from unlanded sync for bug %s (PR %s) will be reapplied" %
                            (sync.bug, sync.pr))
                commits.extend(branch_commits)

        # All the gecko commits that landed between the target sync point and master
        unlanded_commits = self.git_wpt.iter_commits("%s..origin/master" %
                                                     self.wpt_commits.head.sha1)
        seen_bugs = set()
        for commit in unlanded_commits:
            wpt_commit = sync_commit.WptCommit(self.git_wpt, commit)
            gecko_commit = wpt_commit.metadata.get("gecko-commit")
            if gecko_commit:
                git_sha = self.git_gecko.cinnabar.hg2git(gecko_commit)
                commit = sync_commit.GeckoCommit(self.git_gecko, git_sha)
                bug = commit.metadata.get("bugzilla-url")
                if on_integration_branch(commit):
                    if bug and bug not in seen_bugs:
                        logger.info("Commits from landed sync for bug %s will be reapplied" % bug)
                        seen_bugs.add(bug)
                    commits.append(commit.sha1)

        commits = set(commits)

        # Order the commits according to the order in which they landed in gecko
        ordered_commits = []
        for commit in self.git_gecko.iter_commits(self.gecko_integration_branch(),
                                                  paths=env.config["gecko"]["path"]["wpt"]):
            if commit.hexsha in commits:
                ordered_commits.append(commit.hexsha)
                commits.remove(commit.hexsha)
            if not commits:
                break

        return list(reversed(ordered_commits))

    def reapply_local_commits(self, commits):
        if not commits:
            return

        landing_commit = self.gecko_commits[-1]
        if "reapplied-commits" in landing_commit.metadata:
            return
        logger.debug("Reapplying commits: %s" % " ".join(commits))
        git_work_gecko = self.gecko_worktree.get()

        try:
            git_work_gecko.git.cherry_pick(no_commit=True, *commits)
        except git.GitCommandError as e:
            logger.error("Failed to reapply commits:\n%s" % (e))
            err_msg = ("Landing wpt failed because reapplying commits failed:\n%s" % (e,))
            env.bz.comment(self.bug, err_msg)
            raise AbortError(err_msg)

        gecko_commits = [sync_commit.GeckoCommit(self.git_gecko, item) for item in commits]
        metadata = {"reapplied-commits": ", ".join(commit.canonical_rev
                                                   for commit in gecko_commits)}
        new_message = sync_commit.Commit.make_commit_msg(landing_commit.msg, metadata)
        git_work_gecko.git.commit(amend=True, no_edit=True, message=new_message)

    def manifest_update(self):
        git_work = self.gecko_worktree.get()
        mach = Mach(git_work.working_dir)
        mach.wpt_manifest_update()
        if git_work.is_dirty():
            git_work.git.add("testing/web-platform/meta")
            git_work.git.commit(amend=True, no_edit=True)

    def add_metadata(self, sync):
        for item in sync.gecko_commits:
            if (item.metadata.get("wpt-pr") == sync.pr and
                item.metadata.get("wpt-type") == "metadata"):
                return

        if sync.metadata_commit:
            self.gecko_worktree.get().git.cherry_pick(sync.metadata_commit.sha1)

    def apply_prs(self, landable_commits):
        """Main entry point to setting the commits for landing.

        For each upstream PR we want to create a separate commit in the
        gecko repository so that we are preserving a useful subset of the history.
        We also want to prevent divergence from upstream. So for each PR that landed
        upstream since our last sync, we take the following steps:
        1) Copy the state of upstream at the commit where the PR landed over to
           the gecko repo
        2) Reapply any commits that have been made to gecko on the integration branch
           but which are not yet landed upstream on top of the PR
        3) Apply any updated metadata from the downstream sync for the PR.
        """
        unlanded_gecko_commits = self.unlanded_gecko_commits()

        prs_applied = set()
        # Check if this was previously applied
        for item in self.gecko_commits:
            pr_id = item.metadata.get("wpt-pr")
            if pr_id is not None:
                prs_applied.add(pr_id)

        for pr, sync, commits in landable_commits:
            if pr not in prs_applied:
                self.add_pr(pr, commits)
            if unlanded_gecko_commits:
                self.reapply_local_commits(unlanded_gecko_commits)
            self.manifest_update()
            if isinstance(sync, downstream.DownstreamSync):
                self.add_metadata(sync)

    @property
    def metadata_commit(self):
        if self.gecko_commits[-1].metadata["wpt-type"] == "metadata":
            return self.gecko_commits[-1]

    def update_metadata_commit(self):
        if not self.metadata_commit:
            assert all(item.metadata.get("wpt-type") != "metadata" for item in self.gecko_commits)
            git_work = self.gecko_worktree.get()
            metadata = {
                "wpt-pr": self.pr,
                "wpt-type": "metadata"
            }
            msg = sync_commit.Commit.make_commit_msg(
                "Bug %s - Update wpt metadata for update to %s, a=testonly" %
                (self.bug, self.wpt_commits.head.sha1), metadata)
            git_work.git.commit(message=msg, allow_empty=True)
        else:
            git_work.git.commit(allow_empty=True, amend=True, no_edit=True)
        commit = git_work.commit("HEAD")
        return sync_commit.GeckoCommit(self.git_gecko, commit.hexsha)

    def update_metadata(self, log_files):
        """Update the web-platform-tests metadata based on the logs
        generated in a try run.

        :param log_files: List of paths to the raw logs from the try run
        """
        # TODO: this shares a lot of code with downstreaming
        meta_path = env.config["gecko"]["path"]["meta"]

        gecko_work = self.gecko_worktree.get()
        mach = Mach(gecko_work.working_dir)
        logger.debug("Updating metadata")
        mach.wpt_update(*log_files)

        if gecko_work.is_dirty(untracked_files=True, path=meta_path):
            gecko_work.index.add([meta_path])
            self.update_metadata_commit()

    def update_sync_point(self, sync_point):
        """Update the in-tree record of the last sync point."""
        new_sha1 = self.wpt_commits.head.sha1
        if sync_point["upstream"] == new_sha1:
            return
        sync_point["upstream"] = new_sha1
        gecko_work = self.gecko_worktree.get()
        sync_point["local"] = gecko_work.head.commit.hexsha
        with open(os.path.join(gecko_work.working_dir,
                               env.config["gecko"]["path"]["meta"],
                               "mozilla-sync"), "w") as f:
            sync_point.dump(f)
        if gecko_work.is_dirty():
            gecko_work.index.add([os.path.join(env.config["gecko"]["path"]["meta"],
                                               "mozilla-sync")])
            gecko_work.index.commit(
                message="Bug %s - [wpt-sync] Update web-platform-tests to %s" %
                (self.bug, new_sha1))


def push(landing):
    """Push from git_work_gecko to inbound.

    Returns: Tuple of booleans (success, retry)"""
    success = False

    landing_tree = env.config["gecko"]["landing"]
    if not tree.is_open(landing_tree):
            logger.info("%s is closed" % landing_tree)
            # TODO make this auto-retry
            raise AbortError("Tree is closed")
    while not success:
        try:
            landing.git_gecko.remotes.mozilla.push(
                "%s:%s" % (landing.branch_name,
                           landing.gecko_integration_branch().split("/", 1)[1]))
        except git.GitCommandError as e:
            changes = landing.git_gecko.remotes.mozilla.fetch()
            if not changes:
                err = "Pushing update to remote failed:\n%s" % e
                logger.error(err)
                landing.bz.comment(landing.bug, err)
                raise AbortError(err)
            try:
                landing.gecko_rebase(landing.gecko_integration_branch())
            except git.GitCommandError as e:
                err = "Rebase failed:\n%s" % e
                logger.error(err)
                landing.bz.comment(landing.bug, err)
                raise AbortError(err)
        success = True
    landing.finish()


def load_sync_point(git_gecko, git_wpt):
    """Read the last sync point from the batch sync process"""
    mozilla_data = git_gecko.git.show("%s:testing/web-platform/meta/mozilla-sync" %
                                      LandingSync.gecko_integration_branch())
    sync_point = SyncPoint()
    sync_point.loads(mozilla_data)
    return sync_point


def unlanded_wpt_commits_by_pr(git_gecko, git_wpt, prev_wpt_head, wpt_head="origin/master"):
    revish = "%s..%s" % (prev_wpt_head, wpt_head)

    commits_by_pr = []
    legacy_sync_re = re.compile(r"Merge pull request \#\d+ from w3c/sync_[0-9a-fA-F]+")

    for commit in git_wpt.iter_commits(revish,
                                       reverse=True,
                                       first_parent=True):
        commit = sync_commit.WptCommit(git_wpt, commit.hexsha)
        if legacy_sync_re.match(commit.msg):
            continue
        pr = commit.pr()
        if not commits_by_pr or commits_by_pr[-1][0] != pr:
            commits_by_pr.append((pr, []))
        commits_by_pr[-1][1].append(commit)
    return commits_by_pr


def landable_commits(git_gecko, git_wpt, prev_wpt_head, wpt_head="origin/master"):
    pr_commits = unlanded_wpt_commits_by_pr(git_gecko, git_wpt, prev_wpt_head, wpt_head)
    landable_commits = []
    for pr, commits in pr_commits:
        last = False
        if not pr:
            # Assume this was some trivial fixup:
            continue
        # Only check the first commit since later ones could be added upstream in the PR
        if upstream.UpstreamSync.has_metadata(commits[0].msg):
            sync = upstream.UpstreamSync.for_bug(git_gecko,
                                                 git_wpt,
                                                 commits[0].metadata["bugzilla-url"])
        else:
            sync = downstream.DownstreamSync.for_pr(git_gecko, git_wpt, pr)
            if not sync:
                # TODO: schedule a downstream sync for this pr
                logger.info("PR %s has no corresponding sync" % pr)
                last = True
            elif not sync.metadata_ready:
                logger.info("Metadata pending for PR %s" % pr)
                last = True
            if last:
                break
        landable_commits.append((pr, sync, commits))

    if not landable_commits:
        logger.info("No new commits are landable")
        return None

    wpt_head = landable_commits[-1][2][-1].sha1
    logger.info("Landing up to commit %s" % wpt_head)

    return wpt_head, landable_commits


@base.entry_point("landing")
def wpt_push(git_wpt, commits):
    git_wpt.remotes.origin.fetch()
    for commit in commits:
        # This causes the PR to be recorded as a note
        sync_commit.WptCommit(git_wpt, commit).pr()


@base.entry_point("landing")
def land_to_gecko(git_gecko, git_wpt, prev_wpt_head=None):
    update_repositories(git_gecko, git_wpt)

    landings = LandingSync.load_all(git_gecko, git_wpt)
    if len(landings) > 1:
        raise ValueError("Multiple open landing branches")
    landing = landings[0] if landings else None

    sync_point = load_sync_point(git_gecko, git_wpt)

    if landing is None:
        if prev_wpt_head is None:
            prev_wpt_head = sync_point["upstream"]

        landable = landable_commits(git_gecko, git_wpt, prev_wpt_head)
        if landable is None:
            return
        wpt_head, commits = landable
        landing = LandingSync.new(git_gecko, git_wpt, prev_wpt_head, wpt_head)
    else:
        if prev_wpt_head and landing.wpt_commits.base.sha1 != prev_wpt_head:
            raise AbortError("Existing landing base commit %s doesn't match"
                             "supplied previous wpt head %s" % (landing.wpt_commits.base.sha1,
                                                                prev_wpt_head))

        wpt_head, commits = landable_commits(git_gecko,
                                             git_wpt,
                                             landing.wpt_commits.base.sha1,
                                             landing.wpt_commits.head.sha1)
        assert wpt_head == landing.wpt_commits.head.sha1

    landing.apply_prs(commits)

    if landing.latest_try_push is None:
        trypush.TryPush.create(landing, hacks=False,
                               try_cls=trypush.TryFuzzyCommit, exclude=["pgo", "ccov"])
    else:
        logger.info("Got existing try push %s" % landing.latest_try_push)

    for _, sync, _ in commits:
        if isinstance(sync, downstream.DownstreamSync):
            sync.try_notify()

    return landing


@base.entry_point("landing")
def try_push_complete(git_gecko, git_wpt, try_push, sync):
    log_files = try_push.download_raw_logs()
    sync.update_metadata(log_files)

    try_push.status = "complete"

    wpt_head, commits = landable_commits(git_gecko,
                                         git_wpt,
                                         sync.wpt_commits.base.sha1,
                                         sync.wpt_commits.head.sha1)

    sync_point = load_sync_point(git_gecko, git_wpt)
    sync.update_sync_point(sync_point)
    push(sync)
    for _, sync, _ in commits:
        if sync is not None:
            if isinstance(sync, downstream.DownstreamSync):
                # If we can't perform a notification by now it isn't ever going
                # to work
                sync.try_notify()
                if not sync.results_notified:
                    env.bz.comment(sync.bug, "Result changes from PR not available.")
            sync.finish()
