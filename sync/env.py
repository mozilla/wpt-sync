_config = None
_bz = None
_gh_wpt = None


MYPY = False
if MYPY:
    from typing import Dict, Optional, Union
    from sync.bug import Bugzilla, MockBugzilla
    from sync.gh import GitHub, MockGitHub


class Environment(object):
    @property
    def config(self):
        # type: () -> Optional[Dict]
        return _config

    @property
    def bz(self):
        # type: () -> Union[Bugzilla, MockBugzilla]
        return _bz

    @property
    def gh_wpt(self):
        # type: () -> Union[GitHub, MockGitHub]
        return _gh_wpt


def set_env(config, bz, gh_wpt):
    global _config
    global _bz
    global _gh_wpt
    _config = config
    _bz = bz
    _gh_wpt = gh_wpt


def clear_env():
    # Only tests should really do this
    global _config
    global _bz
    global _gh_wpt
    _config = None
    _bz = None
    _gh_wpt = None
