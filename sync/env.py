MYPY = False
if MYPY:
    from typing import Any, Dict, Optional, Text
    from sync.bug import Bugzilla
    from sync.gh import GitHub

_config = None  # type: Optional[Dict]
_bz = None  # type: Optional[Bugzilla]
_gh_wpt = None  # type: Optional[GitHub]


class Environment(object):
    @property
    def config(self):
        # type: () -> Dict[Text, Any]
        assert _config is not None
        return _config

    @property
    def bz(self):
        # type: () -> Bugzilla
        assert _bz is not None
        return _bz

    @property
    def gh_wpt(self):
        # type: () -> GitHub
        assert _gh_wpt is not None
        return _gh_wpt


def set_env(config,  # type: Dict
            bz,  # type: Bugzilla
            gh_wpt  # type: GitHub
            ):
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
