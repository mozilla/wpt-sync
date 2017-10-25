import os
import re
import subprocess

import git
from pipeline import AbortError


METADATA_RE = re.compile("([^\:]*): (.*)")


class ShowError(Exception):
    pass


class ApplyError(Exception):
    pass


class Commit(object):
    def __init__(self, repo, sha1):
        self.repo = repo
        self.sha1 = sha1
        self.commit = self.repo.commit(self.sha1)

    @property
    def canonical_rev(self):
        if hasattr(self.repo, "cinnabar"):
            return self.repo.cinnabar.git2hg(self.sha1)
        return self.sha1

    @property
    def msg(self):
        return self.commit.message

    @property
    def metadata(self):
        lines = self.msg.split("\n")
        metadata = {}
        for line in reversed(lines):
            if not line.strip():
                break
            m = METADATA_RE.match(line)
            if m:
                key, value = m.groups()
                metadata[key] = value
        return metadata

    @classmethod
    def create(cls, repo, msg, metadata):
        msg = Commit.make_commit_msg(msg, metadata)
        commit = repo.index.commit(message=msg)
        return cls(repo, commit.hexsha)

    @staticmethod
    def make_commit_msg(msg, metadata):
        if metadata:
            metadata_str = "\n".join("%s: %s" % item for item in sorted(metadata.items()))
            msg = "%s\n%s" % (msg, metadata_str)
        return msg

    def move(self, dest_repo, skip_empty=True, msg_filter=None, metadata=None, src_prefix=None,
             dest_prefix=None):
        if metadata is None:
            metadata = {}

        if msg_filter:
            msg, metadata_extra = msg_filter(self.msg)
        else:
            msg, metadata_extra = self.msg, None

        if metadata_extra:
            metadata.update(metadata_extra)

        msg = Commit.make_commit_msg(msg, metadata)

        with Store(dest_repo, self.canonical_rev + ".message", msg) as message_path:
            show_args = ()
            if src_prefix:
                show_args = ("--", src_prefix)
            try:
                patch = self.repo.git.show(self.sha1, pretty="email", *show_args) + "\n"
            except git.GitCommandError as e:
                raise AbortError(e.message)

            if skip_empty and patch.endswith("\n\n\n"):
                return None

            strip_dirs = len(src_prefix.split("/")) + 1 if src_prefix else 1

            with Store(dest_repo, self.canonical_rev + ".diff", patch) as patch_path:

                # Without this tests were failing with "Index does not match"
                dest_repo.git.update_index(refresh=True)
                apply_kwargs = {}
                if dest_prefix:
                    apply_kwargs["directory"] = dest_prefix
                try:
                    dest_repo.git.apply(patch_path, index=True, p=strip_dirs, **apply_kwargs)
                except git.GitCommandError as e:
                    err_msg = """git apply failed
        %s returned status %s
        Patch saved as :%s
        Commit message saved as: %s
         %s""" % (e.command, e.status, patch_path, message_path, e.stderr)
                    raise AbortError(err_msg)

                return Commit.create(dest_repo, msg, None)



class Store(object):
    """Create a named file that is deleted if no exception is raised"""

    def __init__(self, repo, name, data):
        self.path = os.path.join(repo.working_dir, name)
        self.data = data

    def __enter__(self):
        with open(self.path, "w") as f:
            f.write(self.data)
        self.data = None
        return self.path

    def __exit__(self, type, value, traceback):
        if not type:
            os.unlink(self.path)
