# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Utility functions for performing various Git functionality."""

from __future__ import absolute_import

import logging
import os
import subprocess
import types

import newrelic
import six

from sync import repos
from sync.env import Environment

MYPY = False
if MYPY:
    from typing import Any, Callable, Dict, List, Optional, Text, Tuple


env = Environment()

logger = logging.getLogger(__name__)


class Command(object):
    """Helper class for running git commands"""

    def __init__(self, name, path):
        """
        :param name: name of the command to call
        :param path: the full path to the command.
        """
        self.name = name
        self.path = path
        self.logger = logger

    def get(self, *subcommand, **opts):
        # type: (*Text, **Any) -> bytes
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
                u"command": self.name,
                u"exit_code": e.returncode,
                u"command_output": e.output})
            raise e

    def __getattr__(self, name):
        # type: (str) -> Callable
        if name.endswith("_"):
            name = name[:-1]

        def call(self, *args, **kwargs):
            # type: (Any, *Text, **Any) -> Text
            return self.get(name.replace("_", "-"), *args, **kwargs)
        call.__name__ = name
        args = (call, self)  # type: Tuple[Any, ...]
        if six.PY2:
            args += (self.__class__,)
        self.__dict__[name] = types.MethodType(*args)
        return self.__dict__[name]


class Mach(Command):
    def __init__(self, path):
        Command.__init__(self, "mach", path)

    def get(self, *subcommand, **opts):
        # type: (*Text, **Any) -> bytes
        state_path = repos.Gecko.get_state_path(env.config, self.path)

        if "env" in opts:
            cmd_env = opts["env"]
        else:
            cmd_env = os.environ.copy()
        cmd_env["MOZBUILD_STATE_PATH"] = state_path
        opts["env"] = cmd_env
        return super(Mach, self).get(*subcommand, **opts)


class WPT(Command):
    def __init__(self, path):
        Command.__init__(self, "wpt", path)


def create_mock(name):
    class MockCommand(Command):
        _data = {}
        _log = []

        def __init__(self, path):
            # type: (Optional[Text]) -> None
            self.name = name
            self.path = path

        @classmethod
        def set_data(cls, command, value):
            # type: (Text, Text) -> None
            cls._data[command] = value

        @classmethod
        def get_log(cls):
            # type: () -> List[Dict[Text, Any]]
            return cls._log

        def get(self, *args, **kwargs):
            # type: (*Text, **Any) -> bytes
            data = self._data.get(args[0], b"")
            if callable(data):
                data = data(*args[1:], **kwargs)

            self._log.append({u"command": self.name,
                              u"cwd": self.path,
                              u"args": args,
                              u"kwargs": kwargs,
                              u"rv": data})

            return data

    return MockCommand
