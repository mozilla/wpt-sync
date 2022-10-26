from __future__ import annotations
from typing import Any, Dict, Optional, Text, TYPE_CHECKING
if TYPE_CHECKING:
    from sync.bug import Bugzilla
    from sync.gh import GitHub

_config: Optional[Dict] = None
_bz: Optional[Bugzilla] = None
_gh_wpt: Optional[GitHub] = None


class Environment:
    @property
    def config(self) -> Dict[Text, Any]:
        assert _config is not None
        return _config

    @property
    def bz(self) -> Bugzilla:
        assert _bz is not None
        return _bz

    @property
    def gh_wpt(self) -> GitHub:
        assert _gh_wpt is not None
        return _gh_wpt


def set_env(config: Dict,
            bz: Bugzilla,
            gh_wpt: GitHub
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
