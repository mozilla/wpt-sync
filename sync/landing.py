import re
import os
import shutil
from collections import defaultdict

import git

import base
import bug
import bugcomponents
import commit as sync_commit
import downstream
import gitutils
import log
import tasks
import tc
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
    status_transitions = [("open", "complete")]

    def __init__(self, git_gecko, git_wpt, process_name):
        super(LandingSync, self).__init__(git_gecko, git_wpt, process_name)
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
    def has_metadata(cls, message):
        required_keys = ["wpt-head",
                         "wpt-type"]
        metadata = sync_commit.get_metadata(message)
        return (all(item in metadata for item in required_keys) and
                metadata["wpt-type"] == "landing")

    def check_finished(self):
        return self.git_gecko.is_ancestor(self.gecko_integration_branch(),
                                          self.branch_name)

    def add_pr(self, pr_id, sync, wpt_commits, copy=True, prev_wpt_head=None):
        if len(wpt_commits) > 1:
            assert all(item.pr() == pr_id for item in wpt_commits)

        # Assume we can always use the author of the first commit
        author = wpt_commits[0].author

        git_work_wpt = self.wpt_worktree.get()
        git_work_gecko = self.gecko_worktree.get()

        pr = env.gh_wpt.get_pull(int(pr_id))

        metadata = {
            "wpt-pr": pr_id,
            "wpt-commits": ", ".join(item.sha1 for item in wpt_commits)
        }

        message = """Bug %s [wpt PR %s] - %s, a=testonly

Automatic update from web-platform-tests%s
"""
        message = message % ((sync and sync.bug) or self.bug,
                             pr.number,
                             pr.title,
                             "\n--\n".join(item.msg for item in wpt_commits) + "\n--")
        message = sync_commit.Commit.make_commit_msg(message, metadata)

        upstream_changed = set()
        for commit in wpt_commits:
            stats = commit.commit.stats
            upstream_changed |= set(stats.files.keys())

        logger.info("Upstream files changed:\n%s" % "\n".join(sorted(upstream_changed)))

        if copy:
            commit = self.copy_pr(git_work_gecko, git_work_wpt, pr, wpt_commits,
                                  message, author)
        else:
            commit = self.move_pr(git_work_gecko, git_work_wpt, pr, wpt_commits,
                                  message, author, prev_wpt_head)

        if commit is not None:
            self.gecko_commits.head = commit

        return commit

    def copy_pr(self, git_work_gecko, git_work_wpt, pr, wpt_commits, message, author):
        # Ensure we have anything in a wpt submodule
        git_work_wpt.git.submodule("update", "--init", "--recursive")

        dest_path = os.path.join(git_work_gecko.working_dir,
                                 env.config["gecko"]["path"]["wpt"])
        src_path = git_work_wpt.working_dir

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
            logger.info("PR %s didn't add any changes" % pr.number)
            return None

        git_work_gecko.git.add(env.config["gecko"]["path"]["wpt"],
                               no_ignore_removal=True)

        commit = git_work_gecko.index.commit(message=message,
                                             author=git.Actor._from_string(author))
        logger.debug("Gecko files changed: \n%s" % "\n".join(commit.stats.files.keys()))
        gecko_commit = sync_commit.GeckoCommit(self.git_gecko, commit.hexsha)

        return gecko_commit

    def move_pr(self, git_work_gecko, git_work_wpt, pr, wpt_commits, message, author,
                prev_wpt_head):
        if prev_wpt_head is None:
            prev_wpt_head = wpt_commits[0].sha1 + "^"

        head = sync_commit.GeckoCommit(self.git_gecko, git_work_gecko.head.commit)
        if head.is_downstream and head.metadata.get("wpt-pr") == str(pr.number):
            return

        applied_commits = [commit.sha1 for commit in wpt_commits]
        metadata = {
            "wpt-pr": pr.number,
            "wpt-commits": ", ".join(applied_commits)
        }
        # Skip any commits that came from upstream since they should have been reapplied
        for commit in wpt_commits:
            if not commit.is_upstream:
                break
            prev_wpt_head = commit.sha1
        else:
            return
        revish = "%s..%s" % (prev_wpt_head, wpt_commits[-1].sha1)
        logger.info("Moving wpt commits %s" % revish)
        return sync_commit.move_commits(self.git_wpt,
                                        revish,
                                        message,
                                        git_work_gecko,
                                        dest_prefix=env.config["gecko"]["path"]["wpt"],
                                        amend=False,
                                        metadata=metadata,
                                        rev_name="pr-%s" % pr.number,
                                        author=wpt_commits[0].author,
                                        exclude={"LICENSE", "resources/testdriver_vendor.js"})

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
            unlanded_syncs = set()
            for status in ["open", "wpt-merged"]:
                unlanded_syncs |= set(upstream.UpstreamSync.load_all(self.git_gecko, self.git_wpt,
                                                                     status=status))

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

        landing_commit = self.gecko_commits[-1]
        git_work_gecko = self.gecko_worktree.get()

        logger.debug("Reapplying commits: %s" % " ".join(item.canonical_rev for item in commits))

        if not commits:
            return

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
                commit = commit.move(git_work_gecko,
                                     msg_filter=msg_filter,
                                     src_prefix=env.config["gecko"]["path"]["wpt"],
                                     dest_prefix=env.config["gecko"]["path"]["wpt"],
                                     three_way=True,
                                     amend=True)
                if commit is None:
                    break

        except AbortError as e:
            err_msg = ("Landing wpt failed because reapplying commits failed:\n%s" % (e.message,))
            env.bz.comment(self.bug, err_msg)
            raise AbortError(err_msg)

    def manifest_update(self):
        git_work = self.gecko_worktree.get()
        mach = Mach(git_work.working_dir)
        mach.wpt_manifest_update()
        if git_work.is_dirty():
            manifest_path = os.path.join(env.config["gecko"]["path"]["meta"],
                                         "MANIFEST.json")
            git_work.git.add(manifest_path)
            git_work.git.commit(amend=True, no_edit=True)
        if git_work.is_dirty():
            # This can happen if the mozilla/meta/MANIFEST.json is updated
            git_work.git.reset(hard=True)

    def has_metadata_for_sync(self, sync):
        for item in reversed(self.gecko_commits):
            if (item.metadata.get("wpt-pr") == sync.pr and
                item.metadata.get("wpt-type") == "metadata"):
                return True
        return False

    def add_metadata(self, sync):
        if sync.skip:
            return

        if self.has_metadata_for_sync(sync):
            return

        if not sync.metadata_commit or sync.metadata_commit.is_empty():
            logger.info("No metadata commit available for PR %s" % sync.pr)
            return

        worktree = self.gecko_worktree.get()

        def handle_empty_commit(e):
            # If git exits with return code 1 and mentions an empty
            # cherry pick, then we tried to cherry pick something
            # that results in an empty commit so reset the index and
            # continue. gitpython doesn't really enforce anything about
            # the type of status, so just convert it to a string to be
            # sure
            if (str(e.status) == "1" and
                "The previous cherry-pick is now empty" in e.stderr):
                logger.info("Cherry pick resulted in an empty commit")
                # If the cherry pick would result in an empty commit,
                # just reset and continue
                worktree.git.reset()
                return True
            return False

        try:
            worktree.git.cherry_pick(sync.metadata_commit.sha1)
            success = True
        except git.GitCommandError as e:
            if handle_empty_commit(e):
                return
            success = False

        if not success:
            logger.info("Cherry-pick failed, trying again with only test-related changes")
            # Try to reset all metadata files that aren't related to an affected test.
            affected_metadata = {os.path.join(env.config["gecko"]["path"]["meta"], item) + ".ini"
                                 for item in sync.affected_tests()}
            checkout = []
            status = gitutils.status(worktree)
            for head_path, data in status.iteritems():
                if data["code"] not in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}:
                    # Only try to reset merge conflicts
                    continue
                path = data["rename"] if data["rename"] else head_path
                if path not in affected_metadata:
                    logger.debug("Resetting changes to %s" % head_path)
                    checkout.append(head_path)
            logger.debug("Resetting changes to %s" % " ".join(checkout))
            worktree.git.checkout("HEAD", "--", *checkout)
            # Now try to commit again
            try:
                worktree.git.commit(c="CHERRY_PICK_HEAD", no_edit=True)
            except git.GitCommandError as e:
                if handle_empty_commit(e):
                    return
                raise

            metadata_commit = worktree.head.commit
            if metadata_commit.message.startswith("Bug None"):
                # If the metadata commit didn't get a valid bug number for some reason,
                # we want to replace the placeholder bug number with the
                # either the sync or landing bug number, otherwise the push will be
                # rejected
                bug_number = sync.bug or self.bug
                new_message = "Bug %s%s" % (bug_number,
                                            metadata_commit.message[len("Bug None"):])
                worktree.git.commit(message=new_message, amend=True)

    def apply_prs(self, prev_wpt_head, landable_commits):
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

        last_pr = None
        has_metadata = False
        if len(self.gecko_commits):
            for commit in reversed(self.gecko_commits):
                if commit.is_landing:
                    return
                if commit.metadata.get("wpt-pr") is not None:
                    last_pr = commit.metadata["wpt-pr"]
                    has_metadata = commit.metadata.get("wpt-type") == "metadata"
                    break

        gecko_commits_landed = set()

        def update_gecko_landed(sync, commits):
            if isinstance(sync, upstream.UpstreamSync):
                for commit in commits:
                    gecko_commit = commit.metadata.get("gecko-commit")
                    if gecko_commit:
                        gecko_commits_landed.add(gecko_commit)

        unapplied_commits = []
        last_applied_seen = last_pr is None
        for i, (pr, sync, commits) in enumerate(landable_commits):
            if last_applied_seen:
                unapplied_commits.append((i, (pr, sync, commits, False)))
            else:
                prev_wpt_head = commits[-1].sha1
                if pr == last_pr:
                    last_applied_seen = True
                    if not has_metadata:
                        unapplied_commits.append((i, (pr, sync, commits, True)))
            update_gecko_landed(sync, commits)

        for i, (pr, sync, commits, meta_only) in unapplied_commits:
            logger.info("Applying PR %i of %i" % (i + 1, len(landable_commits)))
            update_gecko_landed(sync, commits)

            # If copy is set then we copy the commits and reapply in-progress upstream
            # syncs. This is currently always disabled, but the intent was to do this for
            # the first commit to ensure that the possible drift from upstream was limited.
            # However there were some difficulties reapplying all the right commits, so it's
            # disabled until this is worked out.
            # To reenable it change the below line to
            # copy = i == 0
            copy = False
            commit = None
            if not meta_only:
                # If we haven't applied it before then create the initial commit
                commit = self.add_pr(pr, sync, commits, prev_wpt_head=prev_wpt_head,
                                     copy=copy)
            prev_wpt_head = commits[-1].sha1
            if commit:
                if copy:
                    self.reapply_local_commits(gecko_commits_landed)
                self.manifest_update()
            if isinstance(sync, downstream.DownstreamSync):
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

    def update_bug_components(self):
        renames = self.wpt_renames()
        if renames is None:
            return

        gecko_work = self.gecko_worktree.get()
        mozbuild_path = bugcomponents.mozbuild_path(gecko_work)
        if not os.path.exists(mozbuild_path):
            return

        bugcomponents.update(gecko_work, renames)

        if gecko_work.is_dirty(path=mozbuild_path):
            gecko_work.git.add(mozbuild_path, all=True)
            self.update_landing_commit()

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
    """Push from git_work_gecko to inbound."""
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
    # The landing is marked as finished when it reaches central


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


def landable_commits(git_gecko, git_wpt, prev_wpt_head, wpt_head=None, include_incomplete=False):
    """Get the list of commits that are able to land.

    :param prev_wpt_head: The sha1 of the previous wpt commit landed to gecko.
    :param wpt_head: The sha1 of the latest possible commit to land to gecko,
                     or None to use the head of the master branch"
    :param include_incomplete: By default we don't attempt to land anything that
                               hasn't completed a metadata update. This flag disables
                               that and just lands everything up to the specified commit."""
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
            syncs = upstream.UpstreamSync.for_bug(git_gecko,
                                                  git_wpt,
                                                  bug.bug_number_from_url(
                                                      commits[0].metadata["bugzilla-url"]))
            for key, values in syncs.iteritems():
                for sync in values:
                    if commits[0] in sync.gecko_commits:
                        break
                else:
                    sync = None
        else:
            sync = downstream.DownstreamSync.for_pr(git_gecko, git_wpt, pr)
            if not include_incomplete:
                if not sync:
                    # TODO: schedule a downstream sync for this pr
                    logger.info("PR %s has no corresponding sync" % pr)
                    last = True
                elif not (sync.skip or sync.metadata_ready):
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


def current(git_gecko, git_wpt):
    landings = LandingSync.load_all(git_gecko, git_wpt)
    if len(landings) > 1:
        raise ValueError("Multiple open landing branches")
    return landings[0] if landings else None


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
def land_to_gecko(git_gecko, git_wpt, prev_wpt_head=None, new_wpt_head=None,
                  include_incomplete=False):
    """Create or continue a landing of wpt commits to gecko.

    :param prev_wpt_head: The sha1 of the previous wpt commit landed to gecko.
    :param wpt_head: The sha1 of the latest possible commit to land to gecko,
                     or None to use the head of the master branch"
    :param include_incomplete: By default we don't attempt to land anything that
                               hasn't completed a metadata update. This flag disables
                               that and just lands everything up to the specified commit."""
    landing = current(git_gecko, git_wpt)
    sync_point = load_sync_point(git_gecko, git_wpt)

    if landing is None:
        update_repositories(git_gecko, git_wpt)
        if prev_wpt_head is None:
            prev_wpt_head = sync_point["upstream"]

        landable = landable_commits(git_gecko, git_wpt,
                                    prev_wpt_head,
                                    wpt_head=new_wpt_head,
                                    include_incomplete=include_incomplete)
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
                                             landing.wpt_commits.head.sha1,
                                             include_incomplete=include_incomplete)
        assert wpt_head == landing.wpt_commits.head.sha1

    landing.apply_prs(prev_wpt_head, commits)

    landing.update_bug_components()

    landing.update_sync_point(sync_point)

    if landing.latest_try_push is None:
        trypush.TryPush.create(landing, hacks=False,
                               try_cls=trypush.TryFuzzyCommit, exclude=["pgo", "ccov"])
    else:
        logger.info("Got existing try push %s" % landing.latest_try_push)

    for _, sync, _ in commits:
        if isinstance(sync, downstream.DownstreamSync):
            try:
                sync.try_notify()
            except AbortError as e:
                logger.error(e.message)

    return landing


@base.entry_point("landing")
def try_push_complete(git_gecko, git_wpt, try_push, sync, allow_push=True):
    # min rate of job success to proceed with metadata update
    target_rate = 0.7
    retriggered = try_push.retriggered_wpt_states(force_update=True)
    intermittents = []
    if not try_push.success() and not retriggered:
        if try_push.success_rate() < target_rate:
            message = (
                "Latest try push for bug %s has too many failures.\n"
                "See %s"
            ) % (sync.bug, try_push.treeherder_url(try_push.try_rev))
            sync.error = message
            env.bz.comment(sync.bug, message)
            try_push.status = "complete"
            return
        num_new_jobs = try_push.retrigger_failures()
        logger.info("%s new tasks scheduled on try for %s" % (num_new_jobs, sync.bug))
        if num_new_jobs:
            env.bz.comment(sync.bug,
                           ("Retriggered failing web-platform-test tasks on "
                            "try before final metadata update."))
            return
    for name, data in retriggered.iteritems():
        total = float(sum(data["states"].itervalues()))
        # assuming that only failures cause metadata updates
        if data["states"][tc.SUCCESS] / total >= target_rate:
            intermittents.append(name)

    log_files = try_push.download_raw_logs(exclude=intermittents)
    sync.update_metadata(log_files)

    try_push.status = "complete"

    wpt_head, commits = landable_commits(git_gecko,
                                         git_wpt,
                                         sync.wpt_commits.base.sha1,
                                         sync.wpt_commits.head.sha1)

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
                try:
                    sync.try_notify()
                except AbortError as e:
                    logger.error(e.message)
                if not sync.results_notified:
                    env.bz.comment(sync.bug, "Result changes from PR not available.")
            sync.finish()


@base.entry_point("landing")
def gecko_push(git_gecko, git_wpt, repository_name, hg_rev, raise_on_error=False,
               base_rev=None):
    rev = git_gecko.cinnabar.hg2git(hg_rev)
    central_ref = env.config["gecko"]["refs"]["central"]
    last_sync_point, base_commit = LandingSync.prev_gecko_commit(git_gecko,
                                                                 repository_name,
                                                                 base_rev,
                                                                 default=central_ref)

    if base_rev is None and git_gecko.is_ancestor(rev, base_commit.sha1):
        logger.info("Last sync point moved past commit")
        return

    landed_central = git_gecko.is_ancestor(rev, central_ref)

    revish = "%s..%s" % (base_commit.sha1, rev)

    landing_sync = current(git_gecko, git_wpt)
    for commit in git_gecko.iter_commits(revish,
                                         reverse=True,
                                         first_parent=True):
        commit = sync_commit.GeckoCommit(git_gecko, commit.hexsha)
        if landed_central and commit.is_landing:
            syncs = LandingSync.for_bug(git_gecko, git_wpt, commit.bug, flat=True)
            if syncs:
                syncs[0].finish()
        elif commit.is_backout:
            for backed_out, _ in commit.landing_commits_backed_out():
                syncs = LandingSync.for_bug(git_gecko, git_wpt, backed_out.bug, flat=True)
                if syncs:
                    syncs[0].status = "open"
                    syncs[0].error = "Landing was backed out"

    if not git_gecko.is_ancestor(rev, last_sync_point.commit.sha1):
        last_sync_point.commit = rev

    if landing_sync and landing_sync.status == "complete":
        tasks.land.apply_async()
