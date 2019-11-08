import git
import pytest
from sync import index, upstream
from sync.gitutils import update_repositories
from sync.lock import SyncLock


def test_delete(env, git_gecko, git_wpt, upstream_gecko_commit):
    # Do some stuff to create an example sync
    bug = "1234"
    test_changes = {"README": "Change README\n"}
    rev = upstream_gecko_commit(test_changes=test_changes, bug=bug,
                                message="Change README")

    update_repositories(git_gecko, git_wpt, wait_gecko_commit=rev)
    _, _, _ = upstream.gecko_push(git_gecko, git_wpt, "autoland", rev,
                                  raise_on_error=True)

    sync = upstream.UpstreamSync.for_bug(git_gecko, git_wpt, bug, flat=True).pop()
    process_name = sync.process_name

    sync_path = "/".join(str(item) for item in process_name.as_tuple())

    ref = git.Reference(git_gecko, "refs/syncs/data")
    assert ref.commit.tree[sync_path]

    gecko_ref = git.Reference(git_gecko, "refs/heads/%s" % sync_path)
    assert gecko_ref.is_valid()

    wpt_ref = git.Reference(git_wpt, "refs/heads/%s" % sync_path)
    assert wpt_ref.is_valid()

    with SyncLock.for_process(process_name) as lock:
        with sync.as_mut(lock):
            sync.delete()

    for idx_cls in (index.SyncIndex, index.PrIdIndex, index.BugIdIndex):
        idx = idx_cls(git_gecko)
        assert not idx.get(idx.make_key(sync))

    ref = git.Reference(git_gecko, "refs/syncs/data")
    with pytest.raises(KeyError):
        ref.commit.tree[sync_path]

    ref = git.Reference(git_gecko, "refs/heads/%s" % sync_path)
    assert not ref.is_valid()

    ref = git.Reference(git_wpt, "refs/heads/%s" % sync_path)
    assert not ref.is_valid()
