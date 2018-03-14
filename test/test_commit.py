from sync import commit as sync_commit


def test_wpt_empty(git_gecko, local_gecko_commit):
    commit = local_gecko_commit(meta_changes={"test/test1.html.ini": "example change"},
                                other_changes={"example": "example change"})
    gecko_commit = sync_commit.GeckoCommit(git_gecko, commit)
    assert not gecko_commit.is_empty()
    assert not gecko_commit.has_wpt_changes()
