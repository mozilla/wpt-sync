import gc

import pytest

from sync import base


def test_processname_update(git_gecko):
    commit = git_gecko.commit("FETCH_HEAD")

    p = base.ProcessName("test", "subtype", "open", "1")
    assert str(p) == "test/subtype/open/1"
    assert p.name_filter() == "test/subtype/*/1"
    assert p.obj_type == "test"
    assert p.subtype == "subtype"
    assert p.status == "open"
    assert p.obj_id == "1"

    ref = base.DataRefObject.create(git_gecko, p, commit)
    assert p._refs == [ref]

    assert ref.path == "refs/syncs/test/subtype/open/1"
    assert ref.ref is not None
    assert ref.commit.sha1 == commit.hexsha

    p.status = "complete"
    assert str(p) == "test/subtype/complete/1"
    assert ref.path == "refs/syncs/test/subtype/complete/1"
    assert p._refs == [ref]


def test_ref_duplicate(git_gecko):
    commit = git_gecko.commit("FETCH_HEAD")

    def create_initial():
        p = base.ProcessName("test", "subtype", "open", "1")
        base.DataRefObject.create(git_gecko, p, commit)
    create_initial()
    # Ensure that the p object has been gc'd
    gc.collect()

    q = base.ProcessName("test", "subtype", "closed", "1")
    assert q._refs == []
    with pytest.raises(ValueError):
        base.DataRefObject.create(git_gecko, q, commit)


def test_process_name(git_gecko, local_gecko_commit):
    commit = local_gecko_commit(test_changes={"README": "Example change"})
    process_name_no_seq_id = base.ProcessName("sync", "upstream", "open", "1234")
    base.DataRefObject.create(git_gecko, process_name_no_seq_id, commit)

    process_name_seq_id = base.ProcessName.with_seq_id(git_gecko, "syncs", "sync",
                                                       "upstream", "open", "1234")
    assert process_name_seq_id.seq_id == 1
