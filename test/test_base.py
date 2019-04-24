import gc

import pytest

from sync import base
from sync.lock import SyncLock


def test_processname():
    p = base.ProcessName("test", "upstream", "1", "0")
    assert str(p) == "test/upstream/1/0"
    assert p.obj_type == "test"
    assert p.subtype == "upstream"
    assert p.obj_id == "1"
    assert p.seq_id == 0


def test_ref_duplicate(git_gecko):
    def create_initial():
        p = base.ProcessName("sync", "upstream", "1", "0")
        with SyncLock("upstream", None) as lock:
            base.SyncData.create(lock, git_gecko, p, {"test": 1})
    create_initial()
    # Ensure that the p object has been gc'd
    gc.collect()

    q = base.ProcessName("sync", "upstream", "1", "0")
    with pytest.raises(ValueError):
        with SyncLock("upstream", None) as lock:
            base.SyncData.create(lock, git_gecko, q, {"test": 2})


def test_processname_seq_id(git_gecko, local_gecko_commit):
    process_name_no_seq_id = base.ProcessName("sync", "upstream", "1234", "0")
    with SyncLock("upstream", None) as lock:
        base.SyncData.create(lock, git_gecko, process_name_no_seq_id, {"test": 1})

    process_name_seq_id = base.ProcessName.with_seq_id(git_gecko,
                                                       "sync",
                                                       "upstream",
                                                       "1234")
    assert process_name_seq_id.seq_id == 1
