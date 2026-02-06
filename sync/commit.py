from __future__ import annotations
import os
import re
import subprocess

import git
from mozautomation import commitparser
from git.objects.commit import Commit as GitPythonCommit
from pygit2 import Blob, Commit as PyGit2Commit, Oid

from . import log
from .env import Environment
from .errors import AbortError
from .lando import hg2git
from .repos import cinnabar, cinnabar_map, pygit2_get

from typing import Dict
from git.repo.base import Repo
from typing import Any, Callable, Self, Tuple
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sync.upstream import UpstreamSync

MsgFilterFunc = Callable[[bytes], Tuple[bytes, Dict[str, str]]]


env = Environment()
logger = log.get_logger(__name__)

METADATA_RE = re.compile(rb"([^:]+): (.*)")


def get_metadata(msg: bytes) -> dict[str, str]:
    # Since this is data we add, we can be sure it's UTF-8 encoded
    data = {}
    for line in msg.splitlines():
        if line:
            m = METADATA_RE.match(line.strip())
            if m:
                key, value = m.groups()
                data[key.decode("utf8")] = value.decode("utf8")
    return data


def try_filter(msg: bytes) -> bytes:
    # It turns out that the string "try:" is forbidden anywhere in gecko commits,
    # because we (mistakenly) think that this always means it's a try string. So we insert
    # a ZWSP which means that the try syntax regexp doesn't match, but the printable
    # representation of the commit message doesn't change
    try_re = re.compile(rb"(\b)try:")
    msg, _ = try_re.subn("\\1try\u200b:".encode(), msg)
    return msg


def first_non_merge(commits: list[WptCommit]) -> WptCommit:
    for item in commits:
        if not item.is_merge:
            return item
    raise ValueError("All commits were merge commits")


def create_commit(repo: Repo, msg: bytes, **kwargs: Any) -> GitPythonCommit:
    """Commit the current index in repo, with msg as the message and additional kwargs
    from kwargs

    gitpython converts all arguments to strings in a way that doesn't allow
    passing bytestrings in as arguments. But it's important to allow providing
    a message that doesn't have a known encoding since we can't pre-validate that. So
    this re-implements the internals of repo.git.execute to avoid the string conversion"""

    prev_head = repo.head.commit
    exec_kwargs = {k: v for k, v in kwargs.items() if k in git.cmd.execute_kwargs}
    opts_kwargs = {k: v for k, v in kwargs.items() if k not in git.cmd.execute_kwargs}

    cmd: list[str | bytes | None] = [repo.git.GIT_PYTHON_GIT_EXECUTABLE]
    cmd.extend(repo.git._persistent_git_options)
    cmd.append(b"commit")
    cmd.append(b"--message=%s" % msg)
    for name, value in opts_kwargs.items():
        name_bytes = git.cmd.dashify(name).encode("utf8")
        if isinstance(value, str):
            value = value.encode("utf8")

        assert value is None or isinstance(value, (bool, bytes))

        if value is True:
            dashes = b"-" if len(name) == 1 else b"--"
            cmd.append(b"%s%s" % (dashes, name_bytes))
        elif isinstance(value, bytes):
            if len(name) == 1:
                cmd.append(b"-%s" % name_bytes)
                cmd.append(value)
            else:
                cmd.append(b"--%s=%s" % (name_bytes, value))
    repo.git.execute(cmd, **exec_kwargs)

    head = repo.head.commit
    assert prev_head != head
    return head


class GitNotes:
    def __init__(self, commit: Commit) -> None:
        self.commit = commit
        self.pygit2_repo = pygit2_get(commit.repo)
        self._data = self._read()

    def _read(self) -> dict[str, str]:
        try:
            note_sha = self.pygit2_repo.lookup_note(self.commit.sha1).id
            note_data = self.pygit2_repo[note_sha].peel(Blob).data
        except KeyError:
            return {}
        return get_metadata(note_data)

    def _write(self) -> None:
        data = "\n".join("%s: %s" % item for item in self._data.items())
        self.pygit2_repo.create_note(
            data,
            self.pygit2_repo.default_signature,
            self.pygit2_repo.default_signature,
            self.commit.sha1,
            "refs/notes/commits",
            True,
        )

    def __getitem__(self, key: str) -> str:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __delitem__(self, key: str) -> None:
        del self._data[key]
        self._write()

    def __setitem__(self, key: str, value: str) -> None:
        self._data[key] = value
        self._write()


class Commit:
    def __init__(
        self, repo: Repo, commit: str | Commit | GitPythonCommit | PyGit2Commit | Oid
    ) -> None:
        self.repo = repo
        self.pygit2_repo = pygit2_get(repo)
        self.cinnabar = cinnabar_map.get(repo)
        _commit = None
        _pygit2_commit = None
        if hasattr(commit, "hexsha"):
            assert isinstance(commit, GitPythonCommit)
            sha1 = commit.hexsha
            _commit = commit
        elif isinstance(commit, Oid):
            sha1 = str(commit)
        elif hasattr(commit, "sha1"):
            assert isinstance(commit, Commit)
            sha1 = commit.sha1
        elif isinstance(commit, (bytes, str)):
            if isinstance(commit, bytes):
                commit_text: str = commit.decode("ascii")
            else:
                commit_text = commit
            commit_obj = self.pygit2_repo.revparse_single(commit_text)
            sha1 = str(commit_obj.id)
        elif hasattr(commit, "id"):
            assert isinstance(commit, PyGit2Commit)
            sha1 = str(commit.id)
            _pygit2_commit = commit
        else:
            raise ValueError("Unrecognised commit %r (type %s)" % (commit, type(commit)))
        if sha1 not in self.pygit2_repo:
            raise ValueError(f"Commit with SHA1 {sha1} not found")
        self.sha1: str = sha1
        self._commit = _commit
        self._pygit2_commit = _pygit2_commit
        self._notes: GitNotes | None = None

    def __eq__(self, other: Any) -> bool:
        if hasattr(other, "sha1"):
            return self.sha1 == other.sha1
        elif hasattr(other, "hexsha"):
            return self.sha1 == other.hexsha
        else:
            return self.sha1 == other
        return False

    def __ne__(self, other: Any) -> bool:
        return not self == other

    @property
    def commit(self) -> GitPythonCommit:
        if self._commit is None:
            self._commit = self.repo.commit(self.sha1)
        return self._commit

    @property
    def pygit2_commit(self) -> PyGit2Commit:
        if self._pygit2_commit is None:
            self._pygit2_commit = self.pygit2_repo[self.sha1].peel(PyGit2Commit)
        return self._pygit2_commit

    @property
    def notes(self) -> GitNotes:
        if self._notes is None:
            self._notes = GitNotes(self)
        assert self._notes is not None
        return self._notes

    @property
    def canonical_rev(self) -> str:
        if self.cinnabar:
            return self.cinnabar.git2hg(self.sha1)
        return self.sha1

    @property
    def git_rev(self) -> str:
        if self.cinnabar:
            hg_rev = self.cinnabar.git2hg(self.sha1)
            return hg2git(hg_rev)
        return self.sha1

    @property
    def msg(self) -> bytes:
        return self.pygit2_commit.raw_message

    @property
    def author(self) -> bytes:
        author = self.pygit2_commit.author
        name = author.raw_name
        email = author.raw_email if author.email else b"unknown"
        return b"%s <%s>" % (name, email)

    @property
    def email(self) -> bytes:
        author = self.pygit2_commit.author
        return author.raw_email

    @property
    def metadata(self) -> dict[str, str]:
        return get_metadata(self.msg)

    @property
    def is_merge(self) -> bool:
        return len(self.pygit2_commit.parent_ids) > 1

    @classmethod
    def create(
        cls,
        repo: Repo,
        msg: bytes,
        metadata: dict[str, str] | None,
        author: bytes | None = None,
        amend: bool = False,
        allow_empty: bool = False,
    ) -> Self:
        msg = Commit.make_commit_msg(msg, metadata)
        commit_kwargs: dict[str, Any] = {}
        if amend:
            commit_kwargs["amend"] = True
            commit_kwargs["no_edit"] = True
        else:
            if author is not None:
                commit_kwargs["author"] = author
        if allow_empty:
            commit_kwargs["allow_empty"] = True
        commit = create_commit(repo, msg, **commit_kwargs)
        return cls(repo, commit.hexsha)

    @staticmethod
    def make_commit_msg(msg: bytes, metadata: dict[str, str] | None) -> bytes:
        if metadata:
            metadata_text = "\n".join("%s: %s" % item for item in sorted(metadata.items()))
            new_lines = b"\n\n" if not msg.endswith(b"\n") else b"\n"
            msg = b"".join([msg, new_lines, metadata_text.encode("utf8")])
        if isinstance(msg, str):
            msg = msg.encode("utf8")
        return msg

    def is_empty(self, prefix: str | None = None) -> bool:
        if len(self.pygit2_commit.parents) == 1:
            # Fast-path for non-merge commits
            diff = self.pygit2_repo.diff(self.pygit2_commit, self.pygit2_commit.parents[0])
            if not prefix:
                # Empty if there are no deltas in the diff
                return not any(diff.deltas)

            for delta in diff.deltas:
                if delta.old_file.path.startswith(prefix) or delta.new_file.path.startswith(prefix):
                    return False
            return True

        return self.show(src_prefix=prefix, format="", patch=True).strip() == ""

    def tags(self) -> list[str]:
        return [item for item in self.repo.git.tag(points_at=self.sha1).split("\n") if item.strip()]

    def move(
        self,
        dest_repo: Repo,
        skip_empty: bool = True,
        msg_filter: MsgFilterFunc | None = None,
        metadata: dict[str, str] | None = None,
        src_prefix: str | None = None,
        dest_prefix: str | None = None,
        amend: bool = False,
        three_way: bool = True,
        exclude: Any | None = None,
        patch_fallback: bool = False,
        allow_empty: bool = False,
    ) -> Commit | None:
        return _apply_patch(
            self.show(src_prefix),
            self.msg,
            self.canonical_rev,
            dest_repo,
            skip_empty,
            msg_filter,
            metadata,
            src_prefix,
            dest_prefix,
            amend,
            three_way,
            author=self.author,
            exclude=exclude,
            patch_fallback=patch_fallback,
            allow_empty=allow_empty,
        )

    def show(self, src_prefix: str | None = None, **kwargs: Any) -> bytes:
        show_args: tuple[str, ...] = ()
        if src_prefix:
            show_args = ("--", src_prefix)
        try:
            show_kwargs: dict[str, Any] = {"binary": True, "stdout_as_string": False}
            show_kwargs.update(kwargs)
            return self.repo.git.show(self.sha1, *show_args, **show_kwargs) + b"\n"
        except git.GitCommandError as e:
            raise AbortError("git show failed") from e


def move_commits(
    repo: Repo,
    revish: str,
    message: bytes,
    dest_repo: Repo,
    skip_empty: bool = True,
    msg_filter: MsgFilterFunc | None = None,
    metadata: dict[str, str] | None = None,
    src_prefix: str | None = None,
    dest_prefix: str | None = None,
    amend: bool = False,
    three_way: bool = True,
    rev_name: str | None = None,
    author: bytes | None = None,
    exclude: set[str] | None = None,
    patch_fallback: bool = False,
    allow_empty: bool = False,
) -> Commit | None:
    if rev_name is None:
        rev_name = revish
    diff_args: tuple[str, ...] = ()
    if src_prefix:
        diff_args = ("--", src_prefix)
    try:
        patch = (
            repo.git.diff(
                revish,
                binary=True,
                submodule="diff",
                pretty="email",
                stdout_as_string=False,
                *diff_args,
            )
            + b"\n"
        )
        logger.info("Created patch")
    except git.GitCommandError as e:
        raise AbortError("git diff failed") from e

    return _apply_patch(
        patch,
        message,
        rev_name,
        dest_repo,
        skip_empty,
        msg_filter,
        metadata,
        src_prefix,
        dest_prefix,
        amend,
        three_way,
        author=author,
        exclude=exclude,
        patch_fallback=patch_fallback,
        allow_empty=allow_empty,
    )


def _apply_patch(
    patch: bytes,
    message: bytes,
    rev_name: str,
    dest_repo: Repo,
    skip_empty: bool = True,
    msg_filter: MsgFilterFunc | None = None,
    metadata: dict[str, str] | None = None,
    src_prefix: str | None = None,
    dest_prefix: str | None = None,
    amend: bool = False,
    three_way: bool = True,
    author: bytes | None = None,
    exclude: set[str] | None = None,
    patch_fallback: bool = False,
    allow_empty: bool = False,
) -> Commit | None:
    assert isinstance(patch, bytes)

    if skip_empty and (
        not patch
        or patch.isspace()
        or not any(line.startswith(b"diff ") for line in patch.splitlines())
    ):
        return None

    if metadata is None:
        metadata = {}

    if msg_filter:
        msg, metadata_extra = msg_filter(message)
    else:
        msg, metadata_extra = message, {}

    if metadata_extra:
        metadata.update(metadata_extra)

    msg = Commit.make_commit_msg(msg, metadata)

    working_dir = dest_repo.working_dir

    assert working_dir is not None

    with Store(dest_repo, rev_name + ".message", msg) as message_path:
        strip_dirs = len(src_prefix.split("/")) + 1 if src_prefix else 1
        with Store(dest_repo, rev_name + ".diff", patch) as patch_path:
            # Without this tests were failing with "Index does not match"
            dest_repo.git.update_index(refresh=True)
            apply_kwargs: dict[str, Any] = {}
            if dest_prefix:
                apply_kwargs["directory"] = dest_prefix
            if three_way:
                apply_kwargs["3way"] = True
            else:
                apply_kwargs["reject"] = True

            err_msg: str | None = None
            try:
                logger.info("Trying to apply patch")
                dest_repo.git.apply(
                    patch_path, index=True, binary=True, p=strip_dirs, **apply_kwargs
                )
                logger.info("Patch applied")
            except git.GitCommandError as e:
                err_msg = """git apply failed
        {} returned status {}
        Patch saved as :{}
        Commit message saved as: {}
         {}""".format(e.command, e.status, patch_path, message_path, e.stderr)
                if patch_fallback and not dest_repo.is_dirty():
                    dest_repo.git.reset(hard=True)
                    cmd = ["patch", "-p%s" % strip_dirs, "-f", "-r=-", "--no-backup-if-mismatch"]
                    if dest_prefix:
                        cmd.append("--directory=%s" % dest_prefix)
                    logger.info(" ".join(cmd))
                    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                    (stdout, stderr) = proc.communicate(patch)
                    if not proc.returncode == 0:
                        err_msg = "%s\n\nPatch failed (status %i):\nstdout:\n%s\nstderr:\n%s" % (
                            err_msg,
                            proc.returncode,
                            stdout.decode("utf8", "replace") if stdout else "",
                            stderr.decode("utf8", "replace") if stderr else "",
                        )
                    else:
                        err_msg = None
                        prefix = b"+++ "
                        paths = []
                        for line in patch.splitlines():
                            if line.startswith(prefix):
                                path_parts_bytes = line[len(prefix) :].split(b"/")[strip_dirs:]
                                path_parts = [item.decode("utf8") for item in path_parts_bytes]
                                if dest_prefix:
                                    path = os.path.join(dest_prefix, *path_parts)
                                else:
                                    path = os.path.join(*path_parts)
                                paths.append(path)
                        dest_repo.git.add(*paths)
                if err_msg is not None:
                    raise AbortError(err_msg)

            if exclude:
                exclude_paths = [
                    os.path.join(dest_prefix, exclude_path) if dest_prefix else exclude_path
                    for exclude_path in exclude
                ]
                exclude_paths = [
                    item
                    for item in exclude_paths
                    if os.path.exists(os.path.join(working_dir, item))
                ]
                try:
                    dest_repo.git.checkout("HEAD", *exclude_paths)
                except git.GitCommandError as e:
                    logger.info(e)
            try:
                logger.info("Creating commit")
                return Commit.create(
                    dest_repo, msg, None, amend=amend, author=author, allow_empty=allow_empty
                )
            except git.GitCommandError as e:
                if amend and e.status == 1 and "--allow-empty" in e.stdout:
                    logger.warning("Amending commit made it empty, resetting")
                    dest_repo.git.reset("HEAD^")
                    return None
                elif not amend and e.status == 1 and "nothing added to commit" in e.stdout:
                    logger.warning("Commit added no changes to destination repo")
                    return None
                else:
                    dest_repo.git.reset(hard=True)
                    raise


class GeckoCommit(Commit):
    @property
    def bug(self) -> int | None:
        bugs = commitparser.parse_bugs(self.msg.splitlines()[0])
        if len(bugs) > 1:
            logger.warning(
                "Got multiple bugs for commit %s: %s"
                % (self.git_rev, ", ".join(str(item) for item in bugs))
            )
        if not bugs:
            return None
        assert isinstance(bugs[0], int)
        return bugs[0]

    def has_wpt_changes(self) -> bool:
        prefix = env.config["gecko"]["path"]["wpt"]
        return not self.is_empty(prefix)

    @property
    def is_backout(self) -> bool:
        return commitparser.is_backout(self.msg)

    @property
    def is_downstream(self) -> bool:
        from . import downstream

        return downstream.DownstreamSync.has_metadata(self.msg)

    @property
    def is_landing(self) -> bool:
        from . import landing

        return landing.LandingSync.has_metadata(self.msg)

    def commits_backed_out(self) -> tuple[list[GeckoCommit], set[int]]:
        # TODO: should bugs be int here
        commits: list[GeckoCommit] = []
        bugs: list[int] = []
        if self.is_backout:
            nodes_bugs = commitparser.parse_backouts(self.msg)
            if nodes_bugs is None:
                # We think this a backout, but have no idea what it backs out
                # it's not clear how to handle that case so for now we pretend it isn't
                # a backout
                return commits, set(bugs)

            nodes, bugs = nodes_bugs
            # Assuming that all commits are listed.
            for node in nodes:
                git_sha = cinnabar(self.repo).hg2git(node.decode("ascii"))
                commits.append(GeckoCommit(self.repo, git_sha))

        return commits, set(bugs)

    def wpt_commits_backed_out(
        self, exclude_downstream: bool = True, exclude_landing: bool = True
    ) -> tuple[list[GeckoCommit], set[int]]:
        """Get a list of all the wpt commits backed out by the current commit.

        :param exclude_downstream: Exclude commits that were downstreamed
        """

        all_commits, bugs = self.commits_backed_out()
        commits = []
        for commit in all_commits:
            if (
                commit.has_wpt_changes()
                and not (exclude_downstream and commit.is_downstream)
                and not (exclude_landing and commit.is_landing)
            ):
                commits.append(commit)
        return commits, set(bugs)

    def landing_commits_backed_out(self) -> tuple[list[GeckoCommit], set[int]]:
        all_commits, bugs = self.commits_backed_out()
        commits = []
        for commit in all_commits:
            if commit.is_landing:
                commits.append(commit)
        return commits, set(bugs)

    def upstream_sync(self, git_gecko: Repo, git_wpt: Repo) -> UpstreamSync | None:
        from . import upstream

        if "upstream-sync" in self.notes:
            seq_id: int | None = None
            bug_str, seq_id_str = self.notes["upstream-sync"].split(":", 1)
            if seq_id_str == "":
                seq_id = None
            else:
                seq_id = int(seq_id_str)
            bug = int(bug_str)
            syncs = upstream.UpstreamSync.load_by_obj(git_gecko, git_wpt, bug, seq_id=seq_id)
            assert len(syncs) <= 1
            if syncs:
                sync = syncs.pop()
                # TODO: Improve the annotations so that this is implied
                assert isinstance(sync, upstream.UpstreamSync)
                return sync
        return None

    def set_upstream_sync(self, sync: UpstreamSync) -> None:
        from . import upstream

        if not isinstance(sync, upstream.UpstreamSync):
            raise ValueError
        seq_id = sync.seq_id
        if seq_id is None:
            seq_id = ""
        self.notes["upstream-sync"] = f"{sync.bug}:{seq_id}"


class WptCommit(Commit):
    def pr(self) -> int | None:
        pr = None
        if "wpt_pr" not in self.notes or not self.notes["wpt_pr"]:
            tags = [item.rsplit("_", 1)[1] for item in self.tags() if item.startswith("merge_pr_")]
            if tags and len(tags) == 1:
                logger.info("Using tagged PR for commit %s" % self.sha1)
                try:
                    pr = int(tags[0])
                except ValueError:
                    pass

            if pr is None:
                pr = env.gh_wpt.pr_for_commit(self.sha1)

            if pr is not None:
                logger.info("Setting PR to %s" % pr)
                self.notes["wpt_pr"] = str(pr)
        else:
            try:
                pr = int(self.notes["wpt_pr"])
            except ValueError:
                del self.notes["wpt_pr"]
        return pr


class Store:
    """Create a named file that is deleted if no exception is raised"""

    def __init__(self, repo: Repo, name: str, data: bytes) -> None:
        working_dir = repo.working_dir
        assert working_dir is not None
        self.path = os.path.join(working_dir, name)
        self.data: bytes | None = data
        assert isinstance(data, bytes)

    def __enter__(self) -> str:
        assert self.data is not None
        with open(self.path, "wb") as f:
            f.write(self.data)
        self.data = None
        return self.path

    def __exit__(
        self,
        type: type | None,
        value: Exception | None,
        traceback: Any | None,
    ) -> None:
        if not type:
            os.unlink(self.path)
