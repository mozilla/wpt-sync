# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Utility functions for performing various Git functionality."""

import logging
import os
import subprocess
import types
from os import PathLike
from typing import Any, Callable, Mapping, Optional, Tuple

from sync import repos
from sync.env import Environment


env = Environment()

logger = logging.getLogger(__name__)


def get_env(state_path: str, env_template: Optional[Mapping[str, str]] = None) -> Mapping[str, str]:
    if env_template is not None:
        cmd_env = {**env_template}
    else:
        cmd_env = os.environ.copy()
    cmd_env["MOZBUILD_STATE_PATH"] = state_path
    if "UV_REQUIRE_HASHES" in cmd_env:
        del cmd_env["UV_REQUIRE_HASHES"]
    return cmd_env


class Command:
    """Helper class for running git commands"""

    def __init__(self, name: str, path: str | PathLike[str]) -> None:
        """
        :param name: name of the command to call
        :param path: the full path to the command.
        """
        self.name = name
        self.path = path
        self.logger = logger

    def get(self, *subcommand: str, **opts: Any) -> bytes:
        """Run the specified subcommand with `command` and return the result.

        eg. r = mach.get('test-info', 'path/to/test')
        """
        assert subcommand and len(subcommand)
        command = [os.path.join(self.path, self.name)] + list(subcommand)
        logger.info("Running command:\n %s" % " ".join(command))
        return subprocess.check_output(command, cwd=self.path, **opts)

    def __getattr__(self, name: str) -> Callable:
        if name.endswith("_"):
            name = name[:-1]

        def call(self: Any, *args: str, **kwargs: Any) -> str:
            return self.get(name.replace("_", "-"), *args, **kwargs)

        call.__name__ = name
        args: Tuple[Any, ...] = (call, self)
        self.__dict__[name] = types.MethodType(*args)
        return self.__dict__[name]


class Mach(Command):
    def __init__(self, path: str | PathLike[str]) -> None:
        Command.__init__(self, "mach", path)

    def get(self, *subcommand: str, **opts: Any) -> bytes:
        state_path = repos.Gecko.get_state_path(env.config, self.path)

        opts["env"] = get_env(state_path, opts.get("env"))
        return super().get(*subcommand, **opts)


class WPT(Command):
    def __init__(self, path: str) -> None:
        Command.__init__(self, "wpt", path)


def create_mock(name: str) -> type[Command]:
    class MockCommand(Command):
        _data: dict[str, bytes] = {}
        _log: list[dict[str, Any]] = []

        def __init__(self, path: str) -> None:
            self.name = name
            self.path = path

        @classmethod
        def set_data(cls, command: str, value: bytes) -> None:
            cls._data[command] = value

        @classmethod
        def get_log(cls) -> list[dict[str, Any]]:
            return cls._log

        def get(self, *args: str, **kwargs: Any) -> bytes:
            data = self._data.get(args[0], b"")
            if callable(data):
                data = data(*args[1:], **kwargs)

            self._log.append(
                {"command": self.name, "cwd": self.path, "args": args, "kwargs": kwargs, "rv": data}
            )

            return data

    return MockCommand
