import hashlib
import json
import os
from unittest.mock import patch, PropertyMock

import pytest

from sync import commit as sync_commit
from sync.gitutils import update_repositories
from sync.repos import cinnabar


def test_wpt_empty(git_gecko, local_gecko_commit):
    commit = local_gecko_commit(
        meta_changes={"test/test1.html.ini": b"example change"},
        other_changes={"example": b"example change"},
    )
    gecko_commit = sync_commit.GeckoCommit(git_gecko, commit)
    assert not gecko_commit.is_empty()
    assert not gecko_commit.has_wpt_changes()
    assert gecko_commit.is_empty("testing/web-platform/tests")


def test_empty(git_gecko, gecko_worktree):
    gecko_worktree.git.commit(allow_empty=True, message="Empty commit")
    commit = gecko_worktree.head.commit
    gecko_commit = sync_commit.GeckoCommit(git_gecko, commit)
    assert gecko_commit.is_empty()


def test_move_utf16(git_gecko, git_wpt_upstream, git_wpt, wpt_worktree, local_gecko_commit):
    commit = local_gecko_commit(other_changes={"test_file": "\U0001f60a".encode("utf16")})
    gecko_commit = sync_commit.GeckoCommit(git_gecko, commit)

    git_wpt.remotes.origin.fetch()
    git_wpt = wpt_worktree()

    with patch("sync.commit.GeckoCommit.canonical_rev", PropertyMock()) as m:
        m.return_value = gecko_commit.sha1
        wpt_commit = gecko_commit.move(git_wpt)

    assert (
        git_wpt.git.show("%s:test_file" % wpt_commit.sha1, stdout_as_string=False).decode("utf16")
        == "\U0001f60a"
    )


@pytest.mark.parametrize(
    "msg,expected",
    [
        (b"Example", {}),
        (b"wpt-pr: 123", {"wpt-pr": "123"}),
        (b"Example\n\nwpt-pr: 123\nabc: def", {"wpt-pr": "123", "abc": "def"}),
        (b"Foo\n wpt-pr: 123\n\nBar\nwpt-data: foo", {"wpt-pr": "123", "wpt-data": "foo"}),
        (b"wpt-pr: 123\nwpt-pr: 234", {"wpt-pr": "234"}),
    ],
)
def test_metadata(msg, expected):
    assert sync_commit.get_metadata(msg) == expected


def test_commits_backed_out(env, git_gecko, git_wpt, upstream_gecko_commit, upstream_gecko_backout):
    bug = 1234
    test_changes = {"README": b"Change README\n"}

    rev = upstream_gecko_commit(test_changes=test_changes, bug=bug, message=b"Change README")
    git_rev = hashlib.sha1(os.urandom(20)).hexdigest()
    env.lando.hg_to_git[rev] = git_rev
    backout_rev = upstream_gecko_backout([rev], [bug])
    update_repositories(git_gecko, git_wpt, wait_gecko_commit=backout_rev)
    git_rev_cinnabar = cinnabar(git_gecko).hg2git(rev)

    backout_commit = sync_commit.GeckoCommit(git_gecko, cinnabar(git_gecko).hg2git(backout_rev))
    commits, bugs = backout_commit.commits_backed_out()

    assert commits == [sync_commit.GeckoCommit(git_gecko, git_rev_cinnabar)]
    assert bugs == {bug}
    assert json.loads(backout_commit.notes["commits-backed-out"]) == {
        "git_commits": [git_rev_cinnabar],
        "bugs": [bug],
    }

    # Re-getting the property should return the same result without lando
    del env.lando.hg_to_git[rev]
    assert backout_commit.commits_backed_out() == (
        [sync_commit.GeckoCommit(git_gecko, git_rev_cinnabar)],
        {bug},
    )


def test_commits_backed_out_revert(
    env, git_gecko, git_wpt, upstream_gecko_commit, upstream_gecko_revert
):
    bug = 1234
    test_changes = {"README": b"Change README\n"}

    rev = upstream_gecko_commit(test_changes=test_changes, bug=bug, message=b"Change README")
    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    git_rev = hashlib.sha1(os.urandom(20)).hexdigest()
    env.lando.git_to_hg[git_rev] = rev
    env.lando.hg_to_git[rev] = git_rev
    backout_rev = upstream_gecko_revert(f"Bug {bug} - Change README", rev)
    update_repositories(git_gecko, git_wpt, wait_gecko_commit=backout_rev)
    git_rev_cinnabar = cinnabar(git_gecko).hg2git(rev)

    backout_commit = sync_commit.GeckoCommit(git_gecko, cinnabar(git_gecko).hg2git(backout_rev))
    assert backout_commit.commits_backed_out() == (
        [sync_commit.GeckoCommit(git_gecko, git_rev_cinnabar)],
        {bug},
    )
    assert json.loads(backout_commit.notes["commits-backed-out"]) == {
        "git_commits": [git_rev_cinnabar],
        "bugs": [bug],
    }

    # Re-getting the property should return the same result without lando
    del env.lando.hg_to_git[rev]
    assert backout_commit.commits_backed_out() == (
        [sync_commit.GeckoCommit(git_gecko, git_rev_cinnabar)],
        {bug},
    )
