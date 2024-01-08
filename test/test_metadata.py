from sync import wptmeta
from sync.base import ProcessName
from sync.meta import GitReader, NullWriter, Metadata
from sync.lock import SyncLock


def test_read(env, git_wpt_metadata):
    reader = GitReader(git_wpt_metadata)
    writer = NullWriter()

    meta = wptmeta.WptMetadata(reader, writer)
    links = list(meta.iterlinks("/example/test.html"))
    assert len(links) == 1

    assert links[0].url.startswith(env.bz.bz_url)
    assert links[0].product == "firefox"
    assert links[0].test_id == "/example/test.html"
    assert links[0].subtest is None
    assert links[0].status is None


def test_add(env, git_wpt_metadata):
    process_name = ProcessName("sync", "downstream", "1234", 0)
    meta = Metadata(process_name)
    assert len(list(meta.metadata.iterlinks("/example/test.html"))) == 1
    labels = list(meta.metadata.iterlabels("/example/test.html"))
    assert len(labels) == 1
    assert labels[0].label == "test-data"

    with SyncLock.for_process(process_name) as lock:
        with meta.as_mut(lock):
            meta.link_bug("/example/test.html",
                          "%s/show_bug.cgi?id=2345" % env.bz.bz_url,
                          product="firefox")
            meta.link_bug("/example-1/test.html",
                          "%s/show_bug.cgi?id=3456" % env.bz.bz_url,
                          product="firefox")

    assert len(list(meta.iter_bug_links("/example/test.html"))) == 2
    assert len(list(meta.iter_bug_links("/example-1/test.html"))) == 1
    labels = list(meta.metadata.iterlabels("/example/test.html"))
    assert len(labels) == 1
    assert labels[0].label == "test-data"

    # Arrange to reread the metadata from origin/master
    meta = Metadata(process_name)
    links = list(meta.iter_bug_links("/example/test.html"))
    assert len(links) == 2
    assert links[0].test_id == "/example/test.html"
    assert links[0].url == "%s/show_bug.cgi?id=1234" % env.bz.bz_url
    assert links[1].test_id == "/example/test.html"
    assert links[1].url == "%s/show_bug.cgi?id=2345" % env.bz.bz_url

    links_1 = list(meta.iter_bug_links("/example-1/test.html"))
    assert len(links_1) == 1
    assert links_1[0].test_id == "/example-1/test.html"
    assert links_1[0].url == "%s/show_bug.cgi?id=3456" % env.bz.bz_url


def test_update(env, git_wpt_metadata):
    process_name = ProcessName("sync", "downstream", "1234", 0)
    meta = Metadata(process_name)
    links = list(meta.metadata.iterlinks("/example/test.html"))
    assert len(links) == 1
    assert links[0].url == "%s/show_bug.cgi?id=1234" % env.bz.bz_url
    labels = list(meta.metadata.iterlabels("/example/test.html"))
    assert len(labels) == 1
    assert labels[0].label == "test-data"

    with SyncLock.for_process(process_name) as lock:
        with meta.as_mut(lock):
            links[0].url = "%s/show_bug.cgi?id=2345" % env.bz.bz_url

    meta = Metadata(process_name)
    links = list(meta.iter_bug_links("/example/test.html"))
    assert links[0].test_id == "/example/test.html"
    assert links[0].url == "%s/show_bug.cgi?id=2345" % env.bz.bz_url
    labels = list(meta.metadata.iterlabels("/example/test.html"))
    assert len(labels) == 1
    assert labels[0].label == "test-data"


def test_delete(env, git_wpt_metadata):
    process_name = ProcessName("sync", "downstream", "1234", 0)
    meta = Metadata(process_name)
    links = list(meta.metadata.iterlinks("/example/test.html"))
    assert len(links) == 1
    assert links[0].url == "%s/show_bug.cgi?id=1234" % env.bz.bz_url
    labels = list(meta.metadata.iterlabels("/example/test.html"))
    assert len(labels) == 1
    assert labels[0].label == "test-data"

    with SyncLock.for_process(process_name) as lock:
        with meta.as_mut(lock):
            links[0].delete()

    meta = Metadata(process_name)
    links = list(meta.iter_bug_links("/example/test.html"))
    len(links) == 0
    labels = list(meta.metadata.iterlabels("/example/test.html"))
    assert len(labels) == 1
    assert labels[0].label == "test-data"
