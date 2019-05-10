import git

from sync import index


class TestIndex(index.Index):
    name = "test"
    key_fields = ("test1", "test2")
    unique = False

    def load_value(self, value):
        return value


def test_create(env, git_gecko):
    TestIndex.create(git_gecko)
    ref = git.Reference(git_gecko, env.config["sync"]["ref"])
    assert ref.is_valid()
    tree = ref.commit.tree["index/test"]
    assert isinstance(tree, git.Tree)


def test_insert(git_gecko):
    idx = TestIndex.create(git_gecko)
    idx.insert(("key1", "key2"), "some_example_data")
    assert idx.get(("key1", "key2")) == set(["some_example_data"])
    assert idx.get(("key1",)) == set(["some_example_data"])
    assert index.Index.changes is None
    assert idx.__class__.changes is not None
    assert idx.changes is not None
    idx.save()
    assert index.Index.changes is None
    assert not idx.__class__.changes
    assert not idx.changes
    assert idx.get(("key1", "key2")) == set(["some_example_data"])
    assert idx.get(("key1",)) == set(["some_example_data"])


def test_insert_multiple(git_gecko):
    idx = TestIndex.create(git_gecko)
    idx.insert(("key1", "key2"), "some_example_data")
    idx.insert(("key1", "key2"), "more_example_data")
    assert idx.get(("key1", "key2")) == set(["some_example_data",
                                             "more_example_data"])
    assert idx.get(("key1",)) == set(["some_example_data",
                                     "more_example_data"])
    idx.save()
    assert idx.get(("key1", "key2")) == set(["some_example_data",
                                             "more_example_data"])
    assert idx.get(("key1",)) == set(["some_example_data",
                                      "more_example_data"])


def test_delete(git_gecko):
    idx = TestIndex.create(git_gecko)
    idx.insert(("key1", "key2"), "some_example_data")
    assert idx.get(("key1", "key2")) == set(["some_example_data"])
    idx.delete(("key1", "key2"), "some_example_data")
    assert idx.get(("key1", "key2")) == set()
    idx.save()
    assert idx.get(("key1", "key2")) == set()

    # And again with a save after insert
    idx.insert(("key1", "key2"), "some_example_data")
    idx.save()
    assert idx.get(("key1", "key2")) == set(["some_example_data"])
    idx.delete(("key1", "key2"), "some_example_data")
    assert idx.get(("key1", "key2")) == set()
    idx.save()


def test_delete_multiple(git_gecko):
    idx = TestIndex.create(git_gecko)
    idx.insert(("key1", "key2"), "some_example_data")
    idx.insert(("key1", "key2"), "more_example_data")
    idx.delete(("key1", "key2"), "some_example_data")
    assert idx.get(("key1", "key2")) == set(["more_example_data"])
    idx.save()
    assert idx.get(("key1", "key2")) == set(["more_example_data"])


def test_clear(git_gecko):
    idx = TestIndex.create(git_gecko)
    idx.insert(("key1", "key2"), "some_example_data")
    idx.insert(("key1", "key3"), "more_example_data")
    idx.save()
    assert idx.get(("key1", "key2")) == set(["some_example_data"])
    idx.insert(("key1", "key4"), "overwrite data")
    idx.save(overwrite=True)
    assert idx.get(("key1", "key2")) == set()
    assert idx.get(("key1", "key3")) == set()
    assert idx.get(("key1", "key4")) == set(["overwrite data"])
