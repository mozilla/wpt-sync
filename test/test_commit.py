from sync import commit as sync_commit


def test_wpt_empty(git_gecko, local_gecko_commit):
    commit = local_gecko_commit(meta_changes={"test/test1.html.ini": "example change"},
                                other_changes={"example": "example change"})
    gecko_commit = sync_commit.GeckoCommit(git_gecko, commit)
    assert not gecko_commit.is_empty()
    assert not gecko_commit.has_wpt_changes()


def test_metadata_update():
    msg = """Bug 1443987 [wpt PR 9909] - [css-typed-om] Clean up parsing tests., a=testonly

This patch:
- Deletes unused interface.html
- Fixes <div> tag in tests.
- Adds case-sensitivity tests.


Bug: 774887
Change-Id: I194ec7549991bfcd7708e640458246b40fc3ab03
Reviewed-on: https://chromium-review.googlesource.com/952509
Reviewed-by: nainar <nainar@chromium.org>
Commit-Queue: Darren Shen <shend@chromium.org>
Cr-Commit-Position: refs/heads/master@{#541592}

wpt-commits: ab1fd254ca0a97ca7cbca5c9fca853596cd22cfd
wpt-pr: 9909"""

    expected = """Bug 1443987 [wpt PR 9909] - [css-typed-om] Clean up parsing tests., a=testonly

This patch:
- Deletes unused interface.html
- Fixes <div> tag in tests.
- Adds case-sensitivity tests.

Bug: 774887
Change-Id: I194ec7549991bfcd7708e640458246b40fc3ab03
Reviewed-on: https://chromium-review.googlesource.com/952509
Reviewed-by: nainar <nainar@chromium.org>
Commit-Queue: Darren Shen <shend@chromium.org>
Cr-Commit-Position: refs/heads/master@{#541592}
wpt-commits: ab1fd254ca0a97ca7cbca5c9fca853596cd22cfd
wpt-pr: 9910
new: value
"""

    metadata = sync_commit.get_metadata(msg)
    assert metadata["wpt-commits"] == "ab1fd254ca0a97ca7cbca5c9fca853596cd22cfd"
    assert metadata["wpt-pr"] == "9909"
    assert metadata["Change-Id"] == "I194ec7549991bfcd7708e640458246b40fc3ab03"

    metadata["wpt-pr"] = "9910"
    metadata["new"] = "value"

    assert expected == sync_commit.Commit.make_commit_msg(msg, metadata)

    # Should replace metadata not in the metadata set
    metadata = {"wpt-pr": "9910",
                "new": "value"}
    expected = """Bug 1443987 [wpt PR 9909] - [css-typed-om] Clean up parsing tests., a=testonly

This patch:
- Deletes unused interface.html
- Fixes <div> tag in tests.
- Adds case-sensitivity tests.

wpt-pr: 9910
new: value
"""
    assert expected == sync_commit.Commit.make_commit_msg(msg, metadata)
