_config = None
_bz = None
_gh_wpt = None


class lazy_property(object):
    """Property that is set on the object as soon as the
    value is not None"""
    def __init__(self, func, name=None, doc=None):
        self.__name__ = name or func.__name__
        self.__module__ = func.__module__
        self.__doc__ = doc or func.__doc__
        self.func = func

    def __get__(self, obj, type=None):
        if obj is None:
            return self
        value = obj.__dict__.get(self.__name__, None)
        if value is None:
            value = self.func(obj)
            if value is not None:
                obj.__dict__[self.__name__] = value
        return value


class Environment(object):
    @lazy_property
    def config(self):
        return _config

    @lazy_property
    def bz(self):
        return _bz

    @lazy_property
    def gh_wpt(self):
        return _gh_wpt


def set_env(config, bz, gh_wpt):
    global _config
    global _bz
    global _gh_wpt
    _config = config
    _bz = bz
    _gh_wpt = gh_wpt
