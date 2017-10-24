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

    def move(self, dest_repo, msg_filter=None, metadata=None, src_prefix=None, dest_prefix=None):
        if metadata is None:
            metadata = {}

        if msg_filter:
            msg, metadata_extra = msg_filter(self.msg)
        else:
            msg, metadata_extra = self.msg, None

        if metadata_extra:
            metadata.update(metadata_extra)

        msg = Commit.make_commit_msg(msg, metadata)

        message_path = store(dest_repo, self.canonical_rev + ".message", msg)

        show_args = ()
        if src_prefix:
            show_args = ("--", src_prefix)
        try:
            patch = self.repo.git.show(self.sha1, pretty="email", *show_args) + "\n"
        except git.GitCommandError as e:
            raise AbortError(e.message)

        strip_dirs = len(src_prefix.split("/")) + 1 if src_prefix else 1

        patch_path = store(dest_repo, self.canonical_rev + ".diff", patch)

        # Without this tests were failing with "Index does not match"
        dest_repo.git.update_index(refresh=True)
        try:
            dest_repo.git.apply(patch_path, index=True, p=strip_dirs)
        except git.GitCommandError as e:
            err_msg = """git apply failed
%s returned status %s
Patch saved as :%s
Commit message saved as: %s
 %s""" % (e.command, e.status, patch_path, message_path, e.stderr)
            raise AbortError(err_msg)

        commit = Commit.create(dest_repo, msg, None)

        os.unlink(patch_path)
        os.unlink(message_path)

        return commit


def store(repo, name, data):
    path = os.path.join(repo.working_dir, name)
    with open(path, "w") as f:
        f.write(data)
    return path
