import os
import sys

from collections import defaultdict
from collections.abc import Callable
from configparser import RawConfigParser

from typing import Any

_config = None

Config = dict[str, Any]


class ConfigParser(RawConfigParser):
    def optionxform(self, option: str) -> str:
        # make option names case sensitive
        return option


def read_ini(path: str) -> ConfigParser:
    print("Loading config from path %s" % path)
    parser = ConfigParser()
    loaded = parser.read(path)
    if path not in loaded:
        raise ValueError("Failed to load ini file %s" % path)
    return parser


def get_root() -> tuple[str, str]:
    wptsync_root = os.environ.get("WPTSYNC_ROOT")
    wptsync_repo_root = os.environ.get("WPTSYNC_REPO_ROOT")
    if wptsync_root is not None:
        root = os.path.abspath(os.path.normpath(wptsync_root))
    else:
        root = os.path.abspath(os.path.normpath(os.path.join(os.path.dirname(__file__), os.pardir)))

    # In production, we want to store the repos in a different volume
    if wptsync_repo_root:
        repo_root = os.path.abspath(os.path.normpath(wptsync_repo_root))
    else:
        repo_root = root
    return root, repo_root


def load() -> Config:
    global _config
    if _config is None:
        root, _ = get_root()
        ini_path = os.environ.get("WPTSYNC_CONFIG", os.path.join(root, "config", "dev", "sync.ini"))
        creds_path = os.environ.get(
            "WPTSYNC_CREDS", os.path.join(root, "config", "dev", "credentials.ini")
        )
        ini_sync = read_ini(ini_path)
        ini_credentials = read_ini(creds_path)
        _config = load_files(ini_sync, ini_credentials)
    return _config


def load_files(ini_sync: RawConfigParser, ini_credentials: RawConfigParser) -> Config:
    root, repo_root = get_root()

    def nested() -> Config:
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


def configure(f: Callable[[Config, *tuple[Any, ...]], Any]) -> Callable[..., Any]:
    # new relic sets PYTHONPATH in order to import the sitecustomize module it uses at startup
    # But if we get to here that already ran, so delete it. This prevents it being inherited
    # into subprocesses (e.g. git cinnabar) that aren't compatible with it.
    if "PYTHONPATH" in os.environ:
        del os.environ["PYTHONPATH"]

    config = load()

    def inner(*args: Any, **kwargs: Any) -> Any:
        return f(config, *args, **kwargs)

    inner.__name__ = f.__name__
    inner.__doc__ = f.__doc__

    return inner


def set_value(
    config: Config, section: str, name: str, value: str, ini_credentials: RawConfigParser
) -> None:
    target = config[section]

    parts = name.split(".")
    for part in parts[:-1]:
        target = target[part]

    out_value: str | int | bool = value
    if value == "%SECRET%":
        out_value = ini_credentials.get(section, name)

    if "%ROOT%" in value:
        out_value = value.replace("%ROOT%", config["root"])

    if value.startswith("$"):
        env_value = os.environ.get(value[1:])
        if env_value is None:
            raise ValueError(f"Must set environment variable {value}")
        out_value = env_value
    elif value.lower() == "true":
        out_value = True
    elif value.lower() == "false":
        out_value = False
    else:
        try:
            out_value = int(value)
        except ValueError:
            pass
    target[parts[-1]] = out_value
