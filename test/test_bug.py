import pytest

from sync import bug

@pytest.mark.parametrize(("whiteboard", "subtype", "status"),
                         [("", None, None),
                          ("[wptsync upstream]", "upstream", None),
                          ("[wptsync upstream error]", "upstream", "error"),
                          ("[existing property]", None, None),
                          ("[wptsync upstream][existing property]", "upstream", None),
                          ("[first property][wptsync upstream error][second property]",
                           "upstream", "error"),
                          ("no parens", None, None),
                          ("no parens[wptsync upstream]", "upstream", None),
                          ("no parens[wptsync upstream error] more text", "upstream", "error"),
                          ("[wptsync downstream][necko-triaged]", "downstream", None)])
def test_get_sync_data(whiteboard, subtype, status):
    assert bug.get_sync_data(whiteboard) == (subtype, status)


@pytest.mark.parametrize(("whiteboard", "subtype", "status", "output"),
                         [("", "upstream", None, "[wptsync upstream]"),
                          ("", "upstream", "error", "[wptsync upstream error]"),
                          ("[wptsync upstream]", "upstream", "error", "[wptsync upstream error]"),
                          ("[wptsync upstream error]", "upstream", None, "[wptsync upstream]"),
                          ("[wptsync upstream]", "downstream", None, "[wptsync downstream]"),
                          ("[wptsync upstream error]", "downstream", None, "[wptsync downstream]"),
                          ("[wptsync upstream]", "downstream", "error",
                           "[wptsync downstream error]"),
                          ("[existing property]", "upstream", None,
                           "[existing property][wptsync upstream]"),
                          ("[wptsync upstream][existing property]", "upstream", "error",
                           "[wptsync upstream error][existing property]"),
                          ("[first property][wptsync upstream][second property]", "upstream",
                           "error",
                           "[first property][wptsync upstream error][second property]"),
                          ("no parens", "upstream", None, "no parens[wptsync upstream]"),
                          ("no parens[wptsync upstream]", "upstream", "error",
                           "no parens[wptsync upstream error]"),
                          ("no parens[wptsync upstream] more text", "upstream", "error",
                           "no parens[wptsync upstream error] more text"),
                          ("[wptsync downstream][necko-triaged]", "downstream", None,
                           "[wptsync downstream][necko-triaged]")])
def test_set_sync_data(whiteboard, subtype, status, output):
    assert bug.set_sync_data(whiteboard, subtype, status) == output
