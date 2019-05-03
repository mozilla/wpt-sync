# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Utility functions for performing various Git functionality."""

from __future__ import absolute_import, unicode_literals

import logging
import os
import subprocess
import types

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
        """ Run the specified subcommand with `command` and return the result.

        eg. r = mach.get('test-info', 'path/to/test')
        """
        assert subcommand and len(subcommand)
        command = [os.path.join(self.path, self.name)] + list(subcommand)
        logger.info("Running command:\n %s" % " ".join(command))
        return subprocess.check_output(command, cwd=self.path, **opts)

    def __getattr__(self, name):
        if name.endswith("_"):
            name = name[:-1]

        def call(self, *args, **kwargs):
            return self.get(name.replace("_", "-"), *args, **kwargs)
        call.__name__ = name
        self.__dict__[name] = types.MethodType(call, self, self.__class__)
        return self.__dict__[name]


class Mach(Command):
    def __init__(self, path):
        Command.__init__(self, "mach", path)


class WPT(Command):
    def __init__(self, path):
        Command.__init__(self, "wpt", path)


def create_mock(name):
    class MockCommand(Command):
        _data = {}
        _log = []

        def __init__(self, path):
            self.name = name
            self.path = path

        @classmethod
        def set_data(cls, command, value):
            cls._data[command] = value

        @classmethod
        def get_log(cls):
            return cls._log

        def get(self, *args, **kwargs):
            data = self._data.get(args[0], "")
            if callable(data):
                data = data(*args[1:], **kwargs)

            self._log.append({"command": self.name,
                              "cwd": self.path,
                              "args": args,
                              "kwargs": kwargs,
                              "rv": data})

            return data

    return MockCommand
