import os
import shutil

import git

import base
import commit as sync_commit
import downstream
import log
import upstream
from env import Environment
from gitutils import update_repositories
from projectutil import Mach
from pipeline import AbortError


env = Environment()

logger = log.get_logger(__name__)


class SyncPoint(object):
    def __init__(self, data):
        self._items = {}

    def __getitem__(self, key):
        return self._items[key]

    def __setitem__(self, key, value):
        self._items[key] = value

    def load(self, fp):
        with open(fp) as f:
            self.loads(f)

    def loads(self, data):
        for line in data:
            if line:
                key, value = line.split(": ", 1)
                self.data[key] = value

    def dump(self, fp):
        fp.write(self.dumps() + "\n")

    def dumps(self):
        return "\n".join("%s: %s" for key, value in self.data.iteritems())


class SyncLanding(base.SyncProcess):
    sync_type = "landing"
    obj_id = "bug"
    statuses = ("open", "merged")

    @classmethod
    def new(cls, git_gecko, git_wpt, wpt_head, bug=None):
        # There is some chance here we create a bug but never create the branch.
        # Probably need something to clean up orphan bugs

        # The gecko branch is a new one based on master
        gecko_base = cls.gecko_integration_branch()
        gecko_head = cls.gecko_integration_branch()

        wpt_base = cls.wpt_base(git_gecko)

        if bug is None:
            bug = env.bz.new("Update web-platform-tests to %s" % wpt_head,
                             "",
                             "Testing",
                             "web-platform-tests")

        return super(SyncLanding, cls).new(git_gecko, git_wpt,
                                           gecko_base, gecko_head,
                                           wpt_base=wpt_base, wpt_head=wpt_head,
                                           bug=bug)

    def check_finished(self):
        return self.git_gecko.is_ancestor(self.gecko_integration_branch(),
                                          self.branch_name)

    def finish(self):
        self.status = "merged"

    def unlanded_syncs(self):
        syncs = (set(upstream.UpstreamSync.load_all(self.git_gecko, self.git_wpt,
                                                    status="open")) +
                 set(upstream.UpstreamSync.load_all(self.git_gecko, self.git_wpt,
                                                    status="merged")))
        merged_by_bug = {item.bug: item for item in syncs}

        for commit in self.git_wpt.iter_commits("%s..%s" % (self.wpt_head.sha1, "origin/master"),
                                                reverse=True):
            commit = sync_commit.WptCommit(self.git_wpt, commit.sha1)
            bug = commit.metadata.get("bugzilla_url")
            if bug:
                bug_id = bug.rspilt("=", 1)[1]
                if bug_id in merged_by_bug:
                    syncs.remove(merged_by_bug[bug_id])
        return syncs

    def add_pr(self, pr_id, wpt_commits):
        if len(wpt_commits > 1):
            assert all(item.pr() == pr_id for item in wpt_commits)

        target_rev = wpt_commits[-1].sha1

        git_work_wpt = self.wpt_worktree.get()
        git_work_gecko = self.gecko_worktree.get()

        metadata = {
            "wpt-pr": pr_id,
            "wpt-commits": ", ".join(item.sha1 for item in wpt_commits)
        }

        dest_path = os.path.join(git_work_gecko.working_dir,
                                 self.config["gecko"]["path"]["wpt"])

        pr = env.gh_wpt.get_pull(pr_id)

        git_work_gecko.checkout(target_rev)
        shutil.rmtree(dest_path)
        shutil.copytree(git_work_wpt.working_dir, dest_path)

        git_work_gecko.git.add(self.config["gecko"]["path"]["wpt"],
                               no_ignore_removal=True)

        message = """%i - [wpt-sync] %s, a=testonly

        Automatic update from web-platform-tests:
        %s
        """ % (self.bug, pr["title"])
        message = sync_commit.Commit.make_commit_msg(message, metadata)
        commit = git_work_gecko.git.commit(message=message)
        gecko_commit = sync_commit.GeckoCommit(self.git_gecko, commit.hexsha)
        self._gecko_commits.append(commit)

        return gecko_commit

    def unlanded_sync_commits(self, unlanded_syncs):
        commits = []
        for sync in unlanded_syncs:
            commits.extend(item.sha1 for item in sync.gecko_commits)
        # Exclude commits only on autoland
        commits = set(item for item in commits if
                      self.git_gecko.is_ancestor(item.sha1,
                                                 self.gecko_integration_branch()))
        ordered_commits = []
        for item in self.git_gecko.iter_commits(self.gecko_integration_branch(),
                                                paths=env.config["gecko"]["path"]["wpt"]):
            if item.hexsha in commits:
                ordered_commits.append(item.hexsha)
        return reversed(ordered_commits)

    def reapply_local_commits(self, commits):
        landing_commit = self.gecko_commits[-1]
        if "reapplied-commits" in landing_commit.metadata:
            return
        git_work_gecko = self.gecko_worktree.get()

        try:
            git_work_gecko.git.cherry_pick(no_commit=True, *commits)
        except git.GitCommandError as e:
            logger.error("Failed to reapply commits:\n%s" % (e))
            err_msg = ("Landing wpt failed because reapplying commits failed:\n%s" % (e,))
            env.bz.comment(self.bug, err_msg)
            raise AbortError(err_msg)

        metadata = {"reapplied-commits": ", ".join(commit.canonical_rev for commit in commits)}
        new_message = sync_commit.Commit.make_commit_msg(landing_commit.msg, metadata)
        git_work_gecko.git.commit(amend=True, no_edit=True, message=new_message)

    def manifest_update(self, git_work_gecko):
        git_work = self.gecko_worktree.get()
        mach = Mach(git_work.working_dir)
        mach.wpt_manifest_update()
        if git_work.is_dirty():
            git_work.git.add("testing/web-platform/meta")
            git_work.git.commit(amend=True, no_edit=True)

    def add_metadata(self, pr, sync):
        apply_metadata = []
        have_try_pushes = {item.metadata.get("try-push")
                           for item in sync.gecko_commits
                           if "try-push" in item.metadata}
        for commit in sync.metadata_commits:
            if commit.metadata["try-push"] not in have_try_pushes:
                apply_metadata.append(commit.sha1)
        # TODO: so we need to reverse the order here?
        if apply_metadata:
            self.gecko_worktree.get().git.cherry_pick(*apply_metadata)

    def apply_prs(self, landable_commits):
        unlanded_syncs = self.unlanded_syncs()
        unlanded_gecko_commits = self.unlanded_sync_commits(unlanded_syncs)

        prs_applied = set()
        # Check if this was previously applied
        for item in self.gecko_commits:
            pr_id = item.metadata.get("wpt-pr")
            if pr_id is not None:
                prs_applied.add(pr_id)

        for pr, sync, commits in landable_commits:
            if pr not in prs_applied:
                self.add_pr(pr, commits)
            if unlanded_syncs:
                self.reapply_local_commits(unlanded_gecko_commits)
            self.manifest_update()
            if isinstance(sync, downstream.DownstreamSync):
                self.add_metadata(pr, sync)

    def update_sync_point(self):
        sync_point = SyncPoint()
        new_sha1 = self.wpt_commits.head.sha1
        if sync_point["upstream"] == new_sha1:
            return
        sync_point["upstream"] = new_sha1
        gecko_work = self.gecko_worktree.get()
        with open(os.path.join(gecko_work.working_dir,
                               "testing/web-platform/meta/mozilla-sync"), "w") as f:
            sync_point.dump(f)
        gecko_work.index.add("testing/web-platform/meta/mozilla-sync")
        gecko_work.index.commit(
            message="Bug %s - Update web-platform-tests to %s" %
            (self.bug, new_sha1))


def push(landing):
    """Push from git_work_gecko to inbound.

    Returns: Tuple of booleans (success, retry)"""
    success = False
    while not success:
        try:
            landing.git_gecko.remotes.mozilla.push(
                "%s:%s" % (landing.branch_name, landing.integration_branch.split("/", 1)[1]))
        except git.GitCommandError as e:
            changes = landing.git_gecko.remotes.mozilla.fetch()
            if not changes:
                err = "Pushing update to remote failed:\n%s" % e
                logger.error(err)
                landing.bz.comment(landing.bug, err)
                raise AbortError(err)
            try:
                landing.gecko_rebase(landing.integration_branch)
            except git.GitCommandError as e:
                err = "Rebase failed:\n%s" % e
                logger.error(err)
                landing.bz.comment(landing.bug, err)
                raise AbortError(err)
        success = True
    landing.finish()


def wpt_base(git_gecko, git_wpt):
    """Read the last sync point from the batch sync process"""
    mozilla_data = git_gecko.git.show("%s:testing/web-platform/meta/mozilla-sync" %
                                      SyncLanding.gecko_integration_branch())
    return sync_commit.WptCommit(git_wpt, SyncPoint.loads(mozilla_data)["upstream"])


def unlanded_wpt_commits_by_pr(git_gecko, git_wpt):
    revish = "%s..%s" % (wpt_base(git_gecko, git_wpt), "origin/master")

    commits_by_pr = []
    for commit in git_wpt.iter_commits(revish,
                                       reverse=True):
        commit = sync_commit.WptCommit(git_wpt, commit.hexsha)
        pr = commit.pr()
        if commits_by_pr and commits_by_pr[-1][0] != pr:
            commits_by_pr.append((pr, []))
        commits_by_pr[-1][1].append(commit)
    return commits_by_pr


def landable_commits(git_gecko, git_wpt):
    pr_commits = unlanded_wpt_commits_by_pr(git_gecko, git_wpt)
    landable_commits = []
    for pr, commits in pr_commits:
        last = False
        if not pr:
            # Assume this was some trivial fixup:
            continue
        # Only check the first commit since later ones could be added upstream in the PR
        if upstream.UpstreamSync.has_metadata(commits[0]):
            sync = upstream.UpstreamSync.for_bug(commits[0].metadata["bugzilla-url"])
        else:
            sync = downstream.DownstreamSync.for_pr(pr)
            if not sync:
                # TODO: schedule a downstream sync for this pr
                last = True
            if not sync.status == "ready":
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


# Entry point
def wpt_push(git_wpt, commits):
    git_wpt.remotes.origin.fetch()
    for commit in commits:
        # This causes the PR to be recorded as a note
        sync_commit.WptCommit(git_wpt, commit).pr()


# Entry point
def land_to_gecko(git_gecko, git_wpt):
    update_repositories(git_gecko, git_wpt)

    landings = SyncLanding.load(git_gecko, git_wpt)
    if len(landings) > 1:
        raise ValueError("Multiple open landing branches")
    landing = landings[0]

    if landing is None:
        landable = landable_commits(git_gecko, git_wpt)
        if landable is None:
            return
        commits, wpt_head = landable
        landing = SyncLanding.new(git_gecko, git_wpt, wpt_head)

    landing.apply_prs(commits)
    # TODO: Try push here
    landing.update_sync_point()

    push(landing)
