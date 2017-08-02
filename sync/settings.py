import os
from collections import defaultdict
from ConfigParser import RawConfigParser

_config = None

root = os.path.abspath(
    os.path.normpath(
        os.path.join(
            os.path.dirname(__file__),
            os.pardir)))


def read_ini(path):
    parser = RawConfigParser()
    loaded = parser.read(path)
    if not path in loaded:
        raise ValueError("Failed to load ini file %s" % path)
    return parser


def load():
    global _config
    if _config is None:
        ini_sync = read_ini(os.path.join(root, "sync.ini"))
        ini_credentials = read_ini(os.path.join(root, "credentials.ini"))

        _config = load_files(ini_sync, ini_credentials)
    return _config


def load_files(ini_sync, ini_credentials):
    nested = lambda: defaultdict(nested)

    config = nested()
    config["root"] = root

    for section in ini_sync.sections():
        for name, value in ini_sync.items(section):
            set_value(config, section, name, value, ini_credentials)
    return config


def configure(f):
    config = load()

    def inner(*args, **kwargs):
        return f(config, *args, **kwargs)

    inner.__name__ = f.__name__
    inner.__doc__ = f.__doc__

    return inner


def set_value(config, section, name, value, ini_credentials):
    target = config[section]

    parts = name.split(".")
    for part in parts[:-1]:
        target = target[part]

    if value == "%SECRET%":
        value = ini_credentials.get(section, name)

    if value.startswith("$"):
        value = os.environ.get(value[1:])
    elif value.lower() == "true":
        value = True
    elif value.lower() == "false":
        value = False
    else:
        try:
            value = int(value)
        except ValueError:
            pass
    target[parts[-1]] = value


if __name__ == "__main__":
    print load()
