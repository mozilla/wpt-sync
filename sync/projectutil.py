# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Utility functions for performing various Git functionality."""

from __future__ import absolute_import, unicode_literals

import logging
import os
import subprocess

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
        return subprocess.check_output(command, cwd=self.path, **opts)
