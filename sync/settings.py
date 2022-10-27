import os
import sys

from collections import defaultdict
from configparser import RawConfigParser

from typing import Any, Dict, Text

_config = None


def read_ini(path):
    print("Loading config from path %s" % path)
    parser = RawConfigParser()
    # make option names case sensitive
    parser.optionxform = str
    loaded = parser.read(path)
    if path not in loaded:
        raise ValueError("Failed to load ini file %s" % path)
    return parser


def get_root():
    if os.environ.get("WPTSYNC_ROOT"):
        root = os.path.abspath(os.path.normpath(os.environ.get("WPTSYNC_ROOT")))
    else:
        root = os.path.abspath(os.path.normpath(
            os.path.join(os.path.dirname(__file__), os.pardir)))

    # In production, we want to store the repos in a different volume
    if os.environ.get("WPTSYNC_REPO_ROOT"):
        repo_root = os.path.abspath(os.path.normpath(os.environ.get("WPTSYNC_REPO_ROOT")))
    else:
        repo_root = root
    return root, repo_root


def load() -> Dict[Text, Any]:
    global _config
    if _config is None:
        root, _ = get_root()
        ini_path = os.environ.get("WPTSYNC_CONFIG",
                                  os.path.join(root, "config", "dev", "sync.ini"))
        creds_path = os.environ.get("WPTSYNC_CREDS",
                                    os.path.join(root, "config", "dev", "credentials.ini"))
        ini_sync = read_ini(ini_path)
        ini_credentials = read_ini(creds_path)
        _config = load_files(ini_sync, ini_credentials)
    return _config


def load_files(ini_sync, ini_credentials):
    root, repo_root = get_root()

    def nested() -> Dict[Text, Any]:
        return defaultdict(nested)

    config = nested()
    config["root"] = root
    config["repo_root"] = repo_root

    print("WPTSYNC_ROOT: %s" % root, file=sys.stderr)
    print("WPTSYNC_REPO_ROOT: %s" % repo_root, file=sys.stderr)

    for section in ini_sync.sections():
        for name, value in ini_sync.items(section):
            set_value(config, section, name, value, ini_credentials)
    return config


def configure(f):
    # new relic sets PYTHONPATH in order to import the sitecustomize module it uses at startup
    # But if we get to here that already ran, so delete it. This prevents it being inherited
    # into subprocesses (e.g. git cinnabar) that aren't compatible with it.
    if "PYTHONPATH" in os.environ:
        del os.environ["PYTHONPATH"]

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

    if "%ROOT%" in value:
        value = value.replace("%ROOT%", config["root"])

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
