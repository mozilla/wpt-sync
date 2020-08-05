from __future__ import absolute_import
import git
import newrelic

from . import gh
from . import log
from . import repos
from . import worktree
from . import wptmeta

from .env import Environment
from .base import CommitBuilder, iter_tree
from .lock import mut, MutGuard

MYPY = False
if MYPY:
    from typing import Iterable, Iterator, Optional, Text, Tuple
    from git.repo.base import Repo
    from sync.downstream import DownstreamSync
    from sync.base import ProcessName
    from sync.lock import SyncLock
    from sync.wptmeta import MetaLink


env = Environment()
logger = log.get_logger(__name__)


class GitReader(wptmeta.Reader):
    """Reader that works with a Git repository (without a worktree)"""

    def __init__(self, repo, ref="origin/master"):
        # type: (Repo, Text) -> None
        self.repo = repo
        self.repo.remotes.origin.fetch()
        self.pygit2_repo = repos.pygit2_get(repo)
        self.rev = self.pygit2_repo.revparse_single(ref)

    def exists(self, rel_path):
        # type: (Text) -> bool
        return rel_path in self.rev.tree

    def read_path(self, rel_path):
        # type: (Text) -> bytes
        entry = self.rev.tree[rel_path]
        return self.pygit2_repo[entry.id].read_raw()

    def walk(self, rel_path):
        for path, obj in iter_tree(self.pygit2_repo, rel_path, rev=self.rev):
            if obj.type == "blob" and obj.name == "META.yml":
                yield "/".join(path[:-1])


class GitWriter(wptmeta.Writer):
    """Writer that works with a Git repository (without a worktree)"""

    def __init__(self, builder):
        # type: (CommitBuilder) -> None
        self.builder = builder

    def write(self, rel_path, data):
        # type: (Text, bytes) -> None
        self.builder.add_tree({rel_path: data})


class NullWriter(wptmeta.Writer):
    def write(self, rel_path):
        raise NotImplementedError


class Metadata(object):
    def __init__(self,
                 process_name,  # type: ProcessName
                 create_pr=False,  # type: bool
                 branch="master"  # type: Text
                 ):
        # type: (...) -> None
        """Object for working with a wpt-metadata repository without requiring
        a worktree.

        Data is read directly from blobs and changes are represented
        as commits to the master branch. On update changes are
        automatically pushed to the remote origin, and conflicts are
        automatically handled with a retry algorithm.

        This implements the usual locking mechanism and writes occur
        when leaving the mutable scope.

        :param process_name: ProcessName object for the metadata. This
                             will typically be the process name of an
                             in-progress sync, used to lock the
                             metadata for update.
        :param create_pr: Create a PR on the remote for the changes,
                          rather than pushing directly to the branch
        :param branch: Branch to read and/or write to"""

        self.process_name = process_name
        self.create_pr = create_pr
        self.branch = branch

        self._lock = None
        meta_repo = repos.WptMetadata(env.config)
        self.repo = meta_repo.repo()
        self.pygit2_repo = repos.pygit2_get(self.repo)

        self.git_reader = GitReader(self.repo, "origin/%s" % self.branch)
        self.null_writer = NullWriter()
        self.metadata = wptmeta.WptMetadata(self.git_reader,
                                            self.null_writer)

        self.worktree = worktree.Worktree(self.repo, self.process_name)
        self.git_work = None

    def _push(self):
        raise NotImplementedError

    def as_mut(self, lock):
        # type: (SyncLock) -> MutGuard
        return MutGuard(lock, self)

    @property
    def github(self):
        # type: () -> gh.GitHub
        return gh.GitHub(env.config["web-platform-tests"]["github"]["token"],
                         env.config["metadata"]["repo"]["url"])

    @classmethod
    def for_sync(cls, sync, create_pr=False):
        # type: (DownstreamSync, bool) -> Metadata
        return cls(sync.process_name, create_pr=create_pr)

    @property
    def lock_key(self):
        # type: () -> Tuple[Text, Text]
        return (self.process_name.subtype, self.process_name.obj_id)

    def exit_mut(self):
        # type: () -> None
        ref_name = self.process_name.path()
        message = "Gecko sync update"
        retry = 0
        MAX_RETRY = 5
        while retry < MAX_RETRY:
            newrelic.agent.record_custom_event("metadata_update", params={})

            self.repo.remotes.origin.fetch()
            self.pygit2_repo.create_reference(ref_name,
                                              self.pygit2_repo.revparse_single(
                                                  "origin/%s" % self.branch).id,
                                              True)
            commit_builder = CommitBuilder(self.repo,
                                           message,
                                           ref=ref_name)
            with commit_builder as builder:
                self.metadata.writer = GitWriter(builder)
                self.metadata.write()
            assert commit_builder.commit is not None
            if not commit_builder.commit.is_empty():
                logger.info("Pushing metadata commit %s" % commit_builder.commit.sha1)
                remote_ref = self.get_remote_ref()
                try:
                    self.repo.remotes.origin.push("%s:refs/heads/%s" % (ref_name, remote_ref))
                except git.GitCommandError as e:
                    changes = self.repo.remotes.origin.fetch()
                    err = "Pushing update to remote failed:\n%s" % e
                    if not changes:
                        logger.error(err)
                        raise
                else:
                    if self.create_pr:
                        newrelic.agent.record_custom_event("metadata_update_create_pr", params={
                            "branch": self.branch
                        })
                        self.github.create_pull(message,
                                                "Update from bug %s" % self.process_name.obj_id,
                                                self.branch,
                                                remote_ref)
                    break
                retry += 1
            else:
                break

        if retry == MAX_RETRY:
            logger.error("Updating metdata failed")
            raise
        self.pygit2_repo.references.delete(ref_name)
        self.metadata.writer = NullWriter()

    def get_remote_ref(self):
        # type: () -> Text
        if not self.create_pr:
            return self.branch

        base_ref_name = "gecko/%s" % self.process_name.path().replace("/", "-")
        ref_name = base_ref_name
        prefix = "refs/remotes/origin/"
        count = 0
        path = prefix + ref_name
        while path in self.pygit2_repo.references:
            count += 1
            ref_name = "%s-%s" % (base_ref_name, count)
            path = prefix + ref_name
        return ref_name

    @mut()
    def link_bug(self,
                 test_id,  # type: Text
                 bug_url,  # type: Text
                 product="firefox",  # type: Text
                 subtest=None,  # type: Optional[Text]
                 status=None  # type: Optional[Text]
                 ):
        # type: (...) -> None
        """Add a link to a bug to the metadata

        :param test_id: id of the test for which the link applies
        :param bug_url: url of the bug to link to
        "param product: product for which the link applies
        :param subtest: optional subtest for which the link applies
        :param status: optional status for which the link applies"""
        self.metadata.append_link(bug_url,
                                  product=product,
                                  test_id=test_id,
                                  subtest=subtest,
                                  status=status)

    def iterbugs(self,
                 test_id,  # type: Text
                 product="firefox",  # type: Text
                 prefixes=None,  # type: Optional[Iterable[Text]]
                 subtest=None,  # type: Optional[Text]
                 status=None,  # type: Optional[Text]
                 ):
        # type: (...) -> Iterator[MetaLink]
        if prefixes is None:
            prefixes = (env.bz.bz_url,
                        "https://github.com/wpt/web-platform-tests")
        for item in self.metadata.iterlinks(test_id=test_id,
                                            product=product,
                                            subtest=subtest,
                                            status=status):
            if any(item.url.startswith(prefix) for prefix in prefixes):
                yield item
