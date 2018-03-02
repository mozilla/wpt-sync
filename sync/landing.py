import re
import os
import shutil
from collections import defaultdict

import git

import base
import bug
import commit as sync_commit
import downstream
import log
import tree
import load
import trypush
import update
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

    def __init__(self, *args, **kwargs):
        super(LandingSync, self).__init__(*args, **kwargs)
        self._unlanded_gecko_commits = None

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

        # Ensure we have anything in a wpt submodule
        git_work_wpt.git.submodule("update", "--init", "--recursive")

        metadata = {
            "wpt-pr": pr_id,
            "wpt-commits": ", ".join(item.sha1 for item in wpt_commits)
        }

        dest_path = os.path.join(git_work_gecko.working_dir,
                                 env.config["gecko"]["path"]["wpt"])
        src_path = git_work_wpt.working_dir

        pr = env.gh_wpt.get_pull(int(pr_id))

        upstream_changed = set()
        for commit in wpt_commits:
            stats = commit.commit.stats
            upstream_changed |= set(stats.files.keys())

        logger.info("Upstream files changed:\n%s" % "\n".join(sorted(upstream_changed)))

        # Specific paths that should be re-checked out
        keep_paths = {"LICENSE", "resources/testdriver_vendor.js"}
        # file names that are ignored in any part of the tree
        ignore_files = {".git"}

        logger.info("Setting wpt HEAD to %s" % wpt_commits[-1].sha1)
        git_work_wpt.head.reference = wpt_commits[-1].commit
        git_work_wpt.head.reset(index=True, working_tree=True)

        # First remove all files so we handle deletion correctly
        shutil.rmtree(dest_path)

        ignore_paths = defaultdict(set)
        for name in keep_paths:
            src, name = os.path.split(os.path.join(src_path, name))
            ignore_paths[src].add(name)

        def ignore_names(src, names):
            rv = []
            for item in names:
                if item in ignore_files:
                    rv.append(item)
            if src in ignore_paths:
                rv.extend(ignore_paths[src])
            return rv

        shutil.copytree(src_path,
                        dest_path,
                        ignore=ignore_names)

        # Now re-checkout the files we don't want to change
        # checkout-index allows us to ignore files that don't exist
        git_work_gecko.git.checkout_index(*(os.path.join(env.config["gecko"]["path"]["wpt"], item)
                                            for item in keep_paths), force=True, quiet=True)

        if not git_work_gecko.is_dirty(untracked_files=True):
            logger.info("PR %s didn't add any changes" % pr_id)
            return None

        if not git_work_gecko.is_dirty(untracked_files=True):
            logger.info("PR %s didn't add any changes" % pr_id)
            return None

        git_work_gecko.git.add(env.config["gecko"]["path"]["wpt"],
                               no_ignore_removal=True)

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
        """Get a list of gecko commits that correspond to commits which have
        landed on the gecko integration branch, but are not yet merged into the
        upstream commit we are updating to.

        There are two possible sources of such commits:
          * Unlanded PRs. These correspond to upstream syncs with status of "open"
          * Gecko PRs that landed between the wpt commit that we are syncing to
            and latest upstream master.

        :return: List of commits in the order in which they originally landed in gecko"""

        if self._unlanded_gecko_commits is None:
            commits = []

            def on_integration_branch(commit):
                # Calling this continually is O(N*M) where N is the number of unlanded commits
                # and M is the average depth of the commit in the gecko tree
                # If we need a faster implementation one approach would be to store all the
                # commits not on the integration branch and check if this commit is in that set
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

            # All the gecko commits that landed between the base sync point and master
            # We take the base here and then remove upstreamed commits that we are landing
            # as we reach them so that we can get the right diffs for the other PRs
            unlanded_commits = self.git_wpt.iter_commits("%s..origin/master" %
                                                         self.wpt_commits.base.sha1)
            seen_bugs = set()
            for commit in unlanded_commits:
                wpt_commit = sync_commit.WptCommit(self.git_wpt, commit)
                gecko_commit = wpt_commit.metadata.get("gecko-commit")
                if gecko_commit:
                    git_sha = self.git_gecko.cinnabar.hg2git(gecko_commit)
                    commit = sync_commit.GeckoCommit(self.git_gecko, git_sha)
                    bug_number = bug.bug_number_from_url(commit.metadata.get("bugzilla-url"))
                    if on_integration_branch(commit):
                        if bug_number and bug_number not in seen_bugs:
                            logger.info("Commits from landed sync for bug %s will be reapplied" %
                                        bug_number)
                            seen_bugs.add(bug_number)
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

            self._unlanded_gecko_commits = list(reversed(
                [sync_commit.GeckoCommit(self.git_gecko, item) for item in ordered_commits]))
        return self._unlanded_gecko_commits

    def reapply_local_commits(self, gecko_commits_landed):
        # The local commits to apply are everything that hasn't been landed at this
        # point in the process
        commits = [item for item in self.unlanded_gecko_commits()
                   if item.canonical_rev not in gecko_commits_landed]

        if not commits:
            return

        landing_commit = self.gecko_commits[-1]
        logger.debug("Reapplying commits: %s" % " ".join(item.canonical_rev for item in commits))
        git_work_gecko = self.gecko_worktree.get()

        already_applied = landing_commit.metadata.get("reapplied-commits")
        if already_applied:
            already_applied = [item.strip() for item in already_applied.split(",")]
        else:
            already_applied = []
        already_applied_set = set(already_applied)

        unapplied_gecko_commits = [item for item in commits if item.canonical_rev
                                   not in already_applied_set]

        try:
            for i, commit in enumerate(unapplied_gecko_commits):
                def msg_filter(_):
                    msg = landing_commit.msg
                    reapplied_commits = (already_applied +
                                         [commit.canonical_rev for commit in commits[:i + 1]])
                    metadata = {"reapplied-commits": ", ".join(reapplied_commits)}
                    return msg, metadata
                logger.info("Reapplying %s - %s" % (commit.sha1, commit.msg))
                # Passing in a src_prefix here means that we only generate a patch for the
                # part of the commit that affects wpt, but then we need to undo it by adding
                # the same dest prefix
                commit.move(git_work_gecko,
                            msg_filter=msg_filter,
                            src_prefix=env.config["gecko"]["path"]["wpt"],
                            dest_prefix=env.config["gecko"]["path"]["wpt"],
                            three_way=True,
                            amend=True)

        except AbortError as e:
            err_msg = ("Landing wpt failed because reapplying commits failed:\n%s" % (e.message,))
            env.bz.comment(self.bug, err_msg)
            raise AbortError(err_msg)

    def manifest_update(self):
        git_work = self.gecko_worktree.get()
        mach = Mach(git_work.working_dir)
        mach.wpt_manifest_update()
        if git_work.is_dirty():
            git_work.git.add("testing/web-platform/meta")
            git_work.git.commit(amend=True, no_edit=True)

    def has_metadata(self, sync):
        for item in self.gecko_commits:
            if (item.metadata.get("wpt-pr") == sync.pr and
                item.metadata.get("wpt-type") == "metadata"):
                return True
        return False

    def add_metadata(self, sync):
        if self.has_metadata(sync):
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

        prs_applied = {}
        metadata_applied = {}
        # Check if this was previously applied
        for item in self.gecko_commits:
            pr_id = item.metadata.get("wpt-pr")
            if pr_id is not None:
                if item.metadata.get("wpt-type") == "metadata":
                    metadata_applied[pr_id] = item
                else:
                    prs_applied[pr_id] = item

        gecko_commits_landed = set()

        for pr, sync, commits in landable_commits:
            if isinstance(sync, upstream.UpstreamSync):
                for commit in commits:
                    gecko_commit = commit.metadata.get("gecko-commit")
                    if gecko_commit:
                        gecko_commits_landed.add(gecko_commit)
            if pr not in prs_applied:
                # If we haven't applied it before then create the initial commit
                commit = self.add_pr(pr, commits)
                if commit is None:
                    # This means the PR didn't change gecko
                    continue
            if pr not in prs_applied or prs_applied[pr] == self.gecko_commits[-1]:
                # If the head commit is the changes from the PR then reapply all the
                # local changes
                self.reapply_local_commits(gecko_commits_landed)
                self.manifest_update()
            if isinstance(sync, downstream.DownstreamSync) and pr not in metadata_applied:
                self.add_metadata(sync)

    @property
    def landing_commit(self):
        head = self.gecko_commits.head
        if (head.metadata.get("wpt-type") == "landing" and
            head.metadata.get("wpt-head") == self.wpt_commits.head.sha1):
            return head

    def update_landing_commit(self):
        git_work = self.gecko_worktree.get()
        if not self.landing_commit:
            metadata = {
                "wpt-type": "landing",
                "wpt-head": self.wpt_commits.head.sha1
            }
            msg = sync_commit.Commit.make_commit_msg(
                "Bug %s - [wpt-sync] Update web-platform-tests to %s, a=testonly" %
                (self.bug, self.wpt_commits.head.sha1), metadata)
            git_work.git.commit(message=msg, allow_empty=True)
        else:
            git_work.git.commit(allow_empty=True, amend=True, no_edit=True)
        return self.gecko_commits[-1]

    def update_metadata(self, log_files):
        """Update the web-platform-tests metadata based on the logs
        generated in a try run.

        :param log_files: List of paths to the raw logs from the try run
        """
        # TODO: this shares a lot of code with downstreaming
        meta_path = env.config["gecko"]["path"]["meta"]

        gecko_work = self.gecko_worktree.get()
        mach = Mach(gecko_work.working_dir)
        logger.info("Updating metadata")
        mach.wpt_update(*log_files)

        if gecko_work.is_dirty(untracked_files=True, path=meta_path):
            gecko_work.git.add(meta_path, all=True)
            self.update_landing_commit()

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
            self.update_landing_commit()


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


def landable_commits(git_gecko, git_wpt, prev_wpt_head, wpt_head=None):
    if wpt_head is None:
        wpt_head = "origin/master"
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
def wpt_push(git_gecko, git_wpt, commits, create_missing=True):
    prs = set()
    for commit in commits:
        # This causes the PR to be recorded as a note
        commit = sync_commit.WptCommit(git_wpt, commit)
        pr = commit.pr()
        pr = int(pr) if pr else None
        if pr is not None and not upstream.UpstreamSync.has_metadata(commit.msg):
            prs.add(pr)
    if create_missing:
        for pr in prs:
            sync = load.get_pr_sync(git_gecko, git_wpt, pr)
            if not sync:
                # If we don't have a sync for this PR create one
                # It's easiest just to go via the GH API here
                pr_data = env.gh_wpt.get_pull(pr)
                update.update_pr(git_gecko, git_wpt, pr_data)


@base.entry_point("landing")
def land_to_gecko(git_gecko, git_wpt, prev_wpt_head=None, new_wpt_head=None):
    update_repositories(git_gecko, git_wpt)

    landings = LandingSync.load_all(git_gecko, git_wpt)
    if len(landings) > 1:
        raise ValueError("Multiple open landing branches")
    landing = landings[0] if landings else None

    sync_point = load_sync_point(git_gecko, git_wpt)

    if landing is None:
        if prev_wpt_head is None:
            prev_wpt_head = sync_point["upstream"]

        landable = landable_commits(git_gecko, git_wpt, prev_wpt_head,
                                    wpt_head=new_wpt_head)
        if landable is None:
            return
        wpt_head, commits = landable
        landing = LandingSync.new(git_gecko, git_wpt, prev_wpt_head, wpt_head)
    else:
        if prev_wpt_head and landing.wpt_commits.base.sha1 != prev_wpt_head:
            raise AbortError("Existing landing base commit %s doesn't match"
                             "supplied previous wpt head %s" % (landing.wpt_commits.base.sha1,
                                                                prev_wpt_head))
        elif new_wpt_head and landing.wpt_commits.head.sha1 != new_wpt_head:
            raise AbortError("Existing landing head commit %s doesn't match"
                             "supplied wpt head %s" % (landing.wpt_commits.head.sha1,
                                                       new_wpt_head))

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
def try_push_complete(git_gecko, git_wpt, try_push, sync, allow_push=True):
    log_files = try_push.download_raw_logs()
    sync.update_metadata(log_files)

    try_push.status = "complete"

    wpt_head, commits = landable_commits(git_gecko,
                                         git_wpt,
                                         sync.wpt_commits.base.sha1,
                                         sync.wpt_commits.head.sha1)

    sync_point = load_sync_point(git_gecko, git_wpt)
    sync.update_sync_point(sync_point)

    if not allow_push:
        logger.info("Landing in bug %s is ready for push.\n"
                    "Working copy is in %s" % (sync.bug,
                                               sync.gecko_worktree.get().working_dir))
        return

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
