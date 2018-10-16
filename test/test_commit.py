from mock import patch, PropertyMock

from sync import commit as sync_commit


def test_wpt_empty(git_gecko, local_gecko_commit):
    commit = local_gecko_commit(meta_changes={"test/test1.html.ini": "example change"},
                                other_changes={"example": "example change"})
    gecko_commit = sync_commit.GeckoCommit(git_gecko, commit)
    assert not gecko_commit.is_empty()
    assert not gecko_commit.has_wpt_changes()


def test_move_utf16(git_gecko, git_wpt_upstream, git_wpt, wpt_worktree, local_gecko_commit):
    commit = local_gecko_commit(other_changes={"test_file": u"\U0001F60A".encode("utf16")})
    gecko_commit = sync_commit.GeckoCommit(git_gecko, commit)

    git_wpt.remotes.origin.fetch()
    git_wpt = wpt_worktree()

    with patch("sync.commit.GeckoCommit.canonical_rev", PropertyMock()) as m:
        m.return_value = gecko_commit.sha1
        wpt_commit = gecko_commit.move(git_wpt)

    assert git_wpt.git.show("%s:test_file" % wpt_commit.sha1,
                            stdout_as_string=False).decode("utf16") == u"\U0001F60A"
