import pytest
from unittest.mock import patch, PropertyMock

from sync import commit as sync_commit


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
