_config = None
_bz = None
_gh_wpt = None


class Environment(object):
    @property
    def config(self):
        return _config

    @property
    def bz(self):
        return _bz

    @property
    def gh_wpt(self):
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
