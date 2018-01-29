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


def test_try_fuzzy():
    push = trypush.TryFuzzyCommit(None, None, [], 0, include=["web-platform-tests"],
                                  exclude=["pgo", "ccov"])
    assert push.query == "web-platform-tests !pgo !ccov"
