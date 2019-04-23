import gc

import pytest

from sync import base
from sync.lock import SyncLock


def test_processname():
    p = base.ProcessName("test", "upstream", "1")
    assert str(p) == "test/upstream/1"
    assert p.obj_type == "test"
    assert p.subtype == "upstream"
    assert p.obj_id == "1"


def test_ref_duplicate(git_gecko):
    commit = git_gecko.commit("FETCH_HEAD")

    def create_initial():
        p = base.ProcessName("test", "upstream", "1")
        with SyncLock("upstream", None) as lock:
            base.DataRefObject.create(lock, git_gecko, p, commit)
    create_initial()
    # Ensure that the p object has been gc'd
    gc.collect()

    q = base.ProcessName("test", "upstream", "1")
    with pytest.raises(ValueError):
        with SyncLock("upstream", None) as lock:
            base.DataRefObject.create(lock, git_gecko, q, commit)


def test_processname_seq_id(git_gecko, local_gecko_commit):
    commit = local_gecko_commit(test_changes={"README": "Example change"})
    process_name_no_seq_id = base.ProcessName("sync", "upstream", "1234")
    with SyncLock("upstream", None) as lock:
        base.DataRefObject.create(lock, git_gecko, process_name_no_seq_id, commit)

    process_name_seq_id = base.ProcessName.with_seq_id(git_gecko,
                                                       "sync",
                                                       "upstream",
                                                       "1234")
    assert process_name_seq_id.seq_id == 1
