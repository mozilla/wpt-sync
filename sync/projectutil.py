# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Utility functions for performing various Git functionality."""


import logging
import os
import subprocess
import types

import newrelic

from sync import repos
from sync.env import Environment

from typing import Any, Callable, Dict, List, Optional, Tuple


env = Environment()

logger = logging.getLogger(__name__)


class Command:
    """Helper class for running git commands"""

    def __init__(self, name, path):
        """
        :param name: name of the command to call
        :param path: the full path to the command.
        """
        self.name = name
        self.path = path
        self.logger = logger

    def get(self, *subcommand: str, **opts: Any) -> bytes:
        """ Run the specified subcommand with `command` and return the result.

        eg. r = mach.get('test-info', 'path/to/test')
        """
        assert subcommand and len(subcommand)
        command = [os.path.join(self.path, self.name)] + list(subcommand)
        logger.info("Running command:\n %s" % " ".join(command))
        try:
            return subprocess.check_output(command, cwd=self.path, **opts)
        except subprocess.CalledProcessError as e:
            newrelic.agent.record_exception(params={
                "command": self.name,
                "exit_code": e.returncode,
                "command_output": e.output})
            raise e

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
    def __init__(self, path):
        Command.__init__(self, "mach", path)

    def get(self, *subcommand: str, **opts: Any) -> bytes:
        state_path = repos.Gecko.get_state_path(env.config, self.path)

        if "env" in opts:
            cmd_env = opts["env"]
        else:
            cmd_env = os.environ.copy()
        cmd_env["MOZBUILD_STATE_PATH"] = state_path
        opts["env"] = cmd_env
        return super().get(*subcommand, **opts)


class WPT(Command):
    def __init__(self, path):
        Command.__init__(self, "wpt", path)


def create_mock(name):
    class MockCommand(Command):
        _data = {}
        _log = []

        def __init__(self, path: Optional[str]) -> None:
            self.name = name
            self.path = path

        @classmethod
        def set_data(cls, command: str, value: str) -> None:
            cls._data[command] = value

        @classmethod
        def get_log(cls) -> List[Dict[str, Any]]:
            return cls._log

        def get(self, *args: str, **kwargs: Any) -> bytes:
            data = self._data.get(args[0], b"")
            if callable(data):
                data = data(*args[1:], **kwargs)

            self._log.append({"command": self.name,
                              "cwd": self.path,
                              "args": args,
                              "kwargs": kwargs,
                              "rv": data})

            return data

    return MockCommand
