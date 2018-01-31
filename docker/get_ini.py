#!/usr/bin/env python
import sys
from ConfigParser import RawConfigParser


def main():
    if len(sys.argv) != 4:
        raise ValueError("Required three arguments; path, section, option")

    path = sys.argv[1]
    section = sys.argv[2]
    option = sys.argv[3]

    parser = RawConfigParser()
    # make option names case sensitive
    parser.optionxform = str
    loaded = parser.read(path)
    if path not in loaded:
        raise ValueError("%s not loaded" % path)
    value = parser.get(section, option)
    print value


if __name__ == "__main__":
    main()
