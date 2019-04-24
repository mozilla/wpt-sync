import os
import re
import subprocess

import git
from mozautomation import commitparser

import log
from env import Environment
from errors import AbortError


env = Environment()
logger = log.get_logger(__name__)

METADATA_RE = re.compile(r"\s*([^:]*): (.*)")


def get_metadata(text):
    data = {}
    for line in reversed(text.splitlines()):
        if line:
            m = METADATA_RE.match(line)
            if m:
                key, value = m.groups()
                data[key] = value
    return data


def try_filter(msg):
    # It turns out that the string "try:" is forbidden anywhere in gecko commits,
    # because we (mistakenly) think that this always means it's a try string. So we insert
    # a ZWSP which means that the try syntax regexp doesn't match, but the printable
    # representation of the commit message doesn't change
    try_re = re.compile(r"(\b)try:")
    msg, _ = try_re.subn(u"\\1try\u200B:", msg)
    return msg


def first_non_merge(commits):
    for item in commits:
        if not item.is_merge:
            return item


class GitNotes(object):
    def __init__(self, commit):
        self.commit = commit
        self._data = self._read()

    def _read(self):
        try:
            text = self.commit.repo.git.notes("show", self.commit.sha1)
        except git.GitCommandError:
            return {}
        data = get_metadata(text)

        return data

    def __getitem__(self, key):
        return self._data[key]

    def __contains__(self, key):
        return key in self._data

    def __setitem__(self, key, value):
        self._data[key] = value
        if key in self._data:
            data = "\n".join("%s: %s" % item for item in self._data.iteritems())
            self.commit.repo.git.notes("add", "-f", "-m", data, self.commit.sha1)
        else:
            self.commit.repo.git.notes("append", "-m", data, self.commit.sha1)


class Commit(object):
    def __init__(self, repo, commit):
        if isinstance(commit, Commit):
            _commit = commit.commit
        elif isinstance(commit, git.Commit):
            _commit = commit
        else:
            _commit = repo.commit(commit)
        self.repo = repo
        self.commit = _commit
        self.sha1 = self.commit.hexsha
        self._notes = None

    def __eq__(self, other):
        if hasattr(other, "sha1"):
            return self.sha1 == other.sha1
        elif hasattr(other, "hexsha"):
            return self.sha1 == other.hexsha
        else:
            return self.sha1 == other

    def __ne__(self, other):
        return not self == other

    @property
    def notes(self):
        if self._notes is None:
            self._notes = GitNotes(self)
        return self._notes

    @property
    def canonical_rev(self):
        if hasattr(self.repo, "cinnabar"):
            return self.repo.cinnabar.git2hg(self.sha1)
        return self.sha1

    @property
    def msg(self):
        return self.commit.message

    @property
    def author(self):
        author = self.commit.author
        if not isinstance(author, (str, unicode)):
            # This is presumably a gitpython Actor object
            rv = author.name
            if author.email:
                rv = "%s <%s>" % (rv, author.email)
        else:
            rv = author
        return rv

    @property
    def metadata(self):
        return get_metadata(self.msg)

    @property
    def is_merge(self):
        return len(self.commit.parents) > 1

    @classmethod
    def create(cls, repo, msg, metadata, author=None, amend=False):
        msg = Commit.make_commit_msg(msg, metadata)
        commit_kwargs = {}
        if amend:
            commit_kwargs["amend"] = True
            commit_kwargs["no_edit"] = True
        else:
            if author is not None:
                commit_kwargs["author"] = author
        repo.git.commit(message=msg, **commit_kwargs)
        return cls(repo, repo.head.commit.hexsha)

    @staticmethod
    def make_commit_msg(msg, metadata):
        if metadata:
            metadata_str = "\n".join("%s: %s" % item for item in sorted(metadata.items()))
            new_lines = "\n\n" if not msg.endswith("\n") else "\n"
            msg = "".join([msg, new_lines, metadata_str])
        return msg

    def is_empty(self, prefix=None):
        if prefix:
            args = ("--", prefix)
        else:
            args = ()
        return self.repo.git.show(self.sha1,
                                  format="",
                                  patch=True,
                                  stdout_as_string=False,
                                  *args).strip() == ""

    def tags(self):
        return [item for item in self.repo.git.tag(points_at=self.sha1).split("\n")
                if item.strip()]

    def move(self, dest_repo, skip_empty=True, msg_filter=None, metadata=None, src_prefix=None,
             dest_prefix=None, amend=False, three_way=True, exclude=None, patch_fallback=False):

        return _apply_patch(self.show(src_prefix), self.msg, self.canonical_rev, dest_repo,
                            skip_empty, msg_filter, metadata, src_prefix, dest_prefix, amend,
                            three_way, author=self.author, exclude=exclude,
                            patch_fallback=patch_fallback)

    def show(self, src_prefix=None, **kwargs):
        show_args = ()
        if src_prefix:
            show_args = ("--", src_prefix)
        try:
            show_kwargs = {"binary": True,
                           "stdout_as_string": False}
            show_kwargs.update(kwargs)
            return self.repo.git.show(self.sha1, *show_args, **show_kwargs) + "\n"
        except git.GitCommandError as e:
            raise AbortError(e.message)


def move_commits(repo, revish, message, dest_repo, skip_empty=True, msg_filter=None, metadata=None,
                 src_prefix=None, dest_prefix=None, amend=False, three_way=True, rev_name=None,
                 author=None, exclude=None, patch_fallback=False):
    if rev_name is None:
        rev_name = revish
    diff_args = ()
    if src_prefix:
        diff_args = ("--", src_prefix)
    try:
        patch = repo.git.diff(revish, binary=True, submodule="diff",
                              pretty="email", stdout_as_string=False, *diff_args) + "\n"
        logger.info("Created patch")
    except git.GitCommandError as e:
        raise AbortError(e.message)

    return _apply_patch(patch, message, rev_name, dest_repo, skip_empty, msg_filter, metadata,
                        src_prefix, dest_prefix, amend, three_way, author=author, exclude=exclude,
                        patch_fallback=patch_fallback)


def _apply_patch(patch, message, rev_name, dest_repo, skip_empty=True, msg_filter=None,
                 metadata=None, src_prefix=None, dest_prefix=None, amend=False, three_way=True,
                 author=None, exclude=None, patch_fallback=False):
    assert type(patch) == str

    if skip_empty and (not patch or patch.isspace() or
                       not any(line.startswith("diff ") for line in patch.splitlines())):
        return None

    if metadata is None:
        metadata = {}

    if msg_filter:
        msg, metadata_extra = msg_filter(message)
    else:
        msg, metadata_extra = message, None

    if metadata_extra:
        metadata.update(metadata_extra)

    msg = Commit.make_commit_msg(msg, metadata).encode("utf8")

    with Store(dest_repo, rev_name + ".message", msg) as message_path:
        strip_dirs = len(src_prefix.split("/")) + 1 if src_prefix else 1
        with Store(dest_repo, rev_name + ".diff", patch) as patch_path:
            # Without this tests were failing with "Index does not match"
            dest_repo.git.update_index(refresh=True)
            apply_kwargs = {}
            if dest_prefix:
                apply_kwargs["directory"] = dest_prefix
            if three_way:
                apply_kwargs["3way"] = True
            else:
                apply_kwargs["reject"] = True
            try:
                logger.info("Trying to apply patch")
                dest_repo.git.apply(patch_path, index=True, binary=True,
                                    p=strip_dirs, **apply_kwargs)
                logger.info("Patch applied")
            except git.GitCommandError as e:
                err_msg = """git apply failed
        %s returned status %s
        Patch saved as :%s
        Commit message saved as: %s
         %s""" % (e.command, e.status, patch_path, message_path, e.stderr)
                if patch_fallback and not dest_repo.is_dirty():
                    dest_repo.git.reset(hard=True)
                    cmd = ["patch", "-p%s" % strip_dirs, "-f", "-r=-",
                           "--no-backup-if-mismatch"]
                    if dest_prefix:
                        cmd.append("--directory=%s" % dest_prefix)
                    logger.info(" ".join(cmd))
                    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                    (stdout, stderr) = proc.communicate(patch)
                    if not proc.returncode == 0:
                        err_msg = ("%s\n\nPatch failed (status %i):\nstdout:\n%s\nstderr:\n%s" %
                                   (err_msg, proc.returncode, stdout, stderr))
                    else:
                        err_msg = None
                        prefix = "+++ "
                        paths = []
                        for line in patch.splitlines():
                            if line.startswith(prefix):
                                path = "%s/%s" % (
                                    dest_prefix,
                                    "/".join(line[len(prefix):].split("/")[strip_dirs:]))
                                paths.append(path)
                        dest_repo.git.add(*paths)
                if err_msg:
                    raise AbortError(err_msg)

            if exclude:
                exclude_paths = [os.path.join(dest_prefix, exclude_path)
                                 if dest_prefix else exclude_path
                                 for exclude_path in exclude]
                exclude_paths = [item for item in exclude_paths
                                 if os.path.exists(os.path.join(dest_repo.working_dir, item))]
                try:
                    dest_repo.git.checkout("HEAD", *exclude_paths)
                except git.GitCommandError as e:
                    logger.info(e)
            try:
                logger.info("Creating commit")
                return Commit.create(dest_repo, msg, None, amend=amend, author=author)
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
    def bug(self):
        bugs = commitparser.parse_bugs(self.commit.message.split("\n")[0])
        if len(bugs) > 1:
            logger.warning("Got multiple bugs for commit %s: %s" %
                           (self.canonical_rev, ", ".join(str(item) for item in bugs)))
        if not bugs:
            return None
        return str(bugs[0])

    def has_wpt_changes(self):
        prefix = env.config["gecko"]["path"]["wpt"]
        return not self.is_empty(prefix)

    @property
    def is_backout(self):
        return commitparser.is_backout(self.commit.message)

    @property
    def is_downstream(self):
        import downstream
        return downstream.DownstreamSync.has_metadata(self.msg)

    @property
    def is_landing(self):
        import landing
        return landing.LandingSync.has_metadata(self.msg)

    def commits_backed_out(self):
        commits = []
        bugs = []
        if self.is_backout:
            nodes_bugs = commitparser.parse_backouts(self.commit.message)
            if nodes_bugs is None:
                # We think this a backout, but have no idea what it backs out
                # it's not clear how to handle that case so for now we pretend it isn't
                # a backout
                return commits, bugs

            nodes, bugs = nodes_bugs
            # Assuming that all commits are listed.
            for node in nodes:
                git_sha = self.repo.cinnabar.hg2git(node)
                commits.append(GeckoCommit(self.repo, git_sha))

        return commits, set(bugs)

    def wpt_commits_backed_out(self, exclude_downstream=True, exclude_landing=True):
        """Get a list of all the wpt commits backed out by the current commit.

        :param exclude_downstream: Exclude commits that were downstreamed
        """

        all_commits, bugs = self.commits_backed_out()
        commits = []
        for commit in all_commits:
            if (commit.has_wpt_changes() and
                not (exclude_downstream and commit.is_downstream) and
                not (exclude_landing and commit.is_landing)):
                commits.append(commit)
        return commits, set(bugs)

    def landing_commits_backed_out(self):
        all_commits, bugs = self.commits_backed_out()
        commits = []
        for commit in all_commits:
            if commit.is_landing:
                commits.append(commit)
        return commits, set(bugs)

    def upstream_sync(self, git_gecko, git_wpt):
        import upstream
        if "upstream-sync" in self.notes:
            bug, seq_id = self.notes["upstream-sync"].split(":", 1)
            if seq_id == "":
                seq_id = None
            else:
                seq_id = int(seq_id)
            syncs = upstream.UpstreamSync.load_by_obj(git_gecko, git_wpt, bug)
            syncs = {item for item in syncs if item.seq_id == seq_id}
            assert len(syncs) <= 1
            if syncs:
                return syncs.pop()

    def set_upstream_sync(self, sync):
        import upstream
        if not isinstance(sync, upstream.UpstreamSync):
            raise ValueError
        seq_id = sync.seq_id
        if seq_id is None:
            seq_id = ""
        self.notes["upstream-sync"] = "%s:%s" % (sync.bug, seq_id)


class WptCommit(Commit):
    def pr(self):
        if "wpt_pr" not in self.notes:
            tags = [item.rsplit("_", 1)[1] for item in self.tags()
                    if item.startswith("merge_pr_")]
            if tags and len(tags) == 1:
                logger.info("Using tagged PR for commit %s" % self.sha1)
                pr = tags[0]
            else:
                pr = env.gh_wpt.pr_for_commit(self.sha1)
            if not pr:
                pr == ""
            logger.info("Setting PR to %s" % pr)
            self.notes["wpt_pr"] = pr
        pr = self.notes["wpt_pr"]
        try:
            int(pr)
            return pr
        except (TypeError, ValueError):
            return None


class Store(object):
    """Create a named file that is deleted if no exception is raised"""

    def __init__(self, repo, name, data):
        self.path = os.path.join(repo.working_dir, name)
        self.data = data
        assert isinstance(data, str)

    def __enter__(self):
        with open(self.path, "w") as f:
            f.write(self.data)
        self.data = None
        return self.path

    def __exit__(self, type, value, traceback):
        if not type:
            os.unlink(self.path)
