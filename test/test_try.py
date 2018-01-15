from sync import trypush


def test_try_message_no_tests():
    assert trypush.TrySyntaxCommit.try_message() == (
        "try: -b do -p win32,win64,linux64,linux -u web-platform-tests"
        "[linux64-stylo,Ubuntu,10.10,Windows 10] -t none "
        "--artifact")


def test_try_message_no_tests_rebuild():
    assert trypush.TrySyntaxCommit.try_message(rebuild=10) == (
        "try: -b do -p win32,win64,linux64,linux -u web-platform-tests"
        "[linux64-stylo,Ubuntu,10.10,Windows 10] -t none "
        "--artifact --rebuild 10")


# def test_try_message_all_rebuild():
#     expected = (
#         "try: -b do -p win32,win64,linux64,linux -u "
#         "web-platform-tests-reftests,web-platform-tests-wdspec,"
#         "web-platform-tests"
#         "[linux64-stylo,Ubuntu,10.10,Windows 10] "
#         "-t none --artifact --rebuild {}"


# def test_try_message_testharness_invalid():
#     base = "foo"
#     tests_affected = {
#         "invalid_type": ["path1"],
#         "testharness": ["testharnesspath1", "testharnesspath2",
#                         os.path.join(base, "path3")]
#     }
#     expected = (
#         "try: -b do -p win32,win64,linux64,linux -u web-platform-tests"
#         "[linux64-stylo,Ubuntu,10.10,Windows 10] -t none "
#         "--artifact --try-test-paths web-platform-tests:{base}/path1,"
#         "web-platform-tests:{base}/testharnesspath1,"
#         "web-platform-tests:{base}/testharnesspath2,"
#         "web-platform-tests:{base}/path3".format(base=base)
#     )
#     assert downstream.try_message(tests_affected, base=base) == expected


# def test_try_message_wdspec_invalid():
#     base = "foo"
#     tests_affected = {
#         "invalid_type": [os.path.join(base, "path1")],
#         "wdspec": [os.path.join(base, "wdspecpath1")],
#         "invalid_empty": [],
#         "also_invalid": ["path2"],
#     }
#     expected = (
#         "try: -b do -p win32,win64,linux64,linux -u web-platform-tests"
#         "[linux64-stylo,Ubuntu,10.10,Windows 10],"
#         "web-platform-tests-wdspec -t none "
#         "--artifact --try-test-paths web-platform-tests:{base}/path1,"
#         "web-platform-tests:{base}/path2,"
#         "web-platform-tests-wdspec:{base}/wdspecpath1".format(base=base)
#     )
#     assert downstream.try_message(tests_affected, base=base) == expected

