import re
import os
from ast import literal_eval
from collections import defaultdict

import newrelic

from . import log
from .env import Environment
from .projectutil import Mach
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple, Union
from git.repo.base import Repo

logger = log.get_logger(__name__)
env = Environment()

# Copied from mozpack.path
re_cache = {}


def match(path: str, pattern: str) -> bool:
    """
    Return whether the given path matches the given pattern.
    An asterisk can be used to match any string, including the null string, in
    one part of the path:
        'foo' matches '*', 'f*' or 'fo*o'
    However, an asterisk matching a subdirectory may not match the null string:
        'foo/bar' does *not* match 'foo/*/bar'
    If the pattern matches one of the ancestor directories of the path, the
    patch is considered matching:
        'foo/bar' matches 'foo'
    Two adjacent asterisks can be used to match files and zero or more
    directories and subdirectories.
        'foo/bar' matches 'foo/**/bar', or '**/bar'
    """
    if not pattern:
        return True
    if pattern not in re_cache:
        p = re.escape(pattern)
        p = re.sub(r"(^|\\\/)\\\*\\\*\\\/", r"\1(?:.+/)?", p)
        p = re.sub(r"(^|\\\/)\\\*\\\*$", r"(?:\1.+)?", p)
        p = p.replace(r"\*", "[^/]*") + "(?:/.*)?$"
        re_cache[pattern] = re.compile(p)
    return re_cache[pattern].match(path) is not None


def remove_obsolete(path: str, moves: Optional[Dict[str, str]] = None) -> str:
    from lib2to3 import pygram, pytree, patcomp  # type: ignore
    from lib2to3.pgen2 import driver

    files_pattern = "with_stmt< 'with' power< 'Files' trailer< '(' arg=any any* ')' > any* > any* >"
    base_dir = os.path.dirname(path) or "."
    d = driver.Driver(pygram.python_grammar, convert=pytree.convert)
    tree = d.parse_file(path)
    pc = patcomp.PatternCompiler()
    pat = pc.compile_pattern(files_pattern)

    unmatched_patterns = set()
    node_patterns = {}

    for node in tree.children:
        match_values: Dict[Any, Any] = {}
        if pat.match(node, match_values):
            path_pat = literal_eval(match_values["arg"].value)
            unmatched_patterns.add(path_pat)
            node_patterns[path_pat] = (node, match_values)

    for base_path, _, files in os.walk(base_dir):
        for filename in files:
            full_path = os.path.join(base_path, filename)
            path = os.path.relpath(full_path, base_dir)
            try:
                assert "../" not in path and not path.endswith("/.."), (
                    "Path {} is outside {}".format(full_path, base_dir)
                )
            except AssertionError:
                newrelic.agent.record_exception(params={"path": full_path})
                continue

            if path[:2] == "./":
                path = path[2:]
            for pattern in unmatched_patterns.copy():
                if match(path, pattern):
                    unmatched_patterns.remove(pattern)

    if moves:
        moved_patterns = compute_moves(moves, unmatched_patterns)
        unmatched_patterns -= set(moved_patterns.keys())
        for old_pattern, new_pattern in moved_patterns.items():
            node, match_values = node_patterns[old_pattern]
            arg = match_values["arg"]
            arg.replace(arg.__class__(arg.type, '"%s"' % new_pattern))

    for pattern in unmatched_patterns:
        logger.debug("Removing %s" % pattern)
        node_patterns[pattern][0].remove()

    return str(tree)


def compute_moves(moves: Dict[str, str], unmatched_patterns: Set[str]) -> Dict[str, str]:
    updated_patterns = {}
    dest_paths = defaultdict(list)
    for pattern in unmatched_patterns:
        # Make things simpler by only considering patterns matching subtrees
        # or single-file patterns
        if "*" in pattern and not pattern.endswith("/**"):
            continue
        for from_path, to_path in moves.items():
            if match(from_path, pattern):
                dest_paths[pattern].append(to_path)

    for pattern, paths in dest_paths.items():
        if "*" not in pattern:
            assert len(paths) == 1
            updated_patterns[pattern] = paths[0]
        elif pattern.endswith("/**"):
            prefix = os.path.commonprefix(paths)
            if not prefix:
                continue
            if not prefix.endswith("/"):
                prefix = os.path.dirname(prefix)
                if not prefix:
                    continue
                prefix += "/"

            updated_patterns[pattern] = prefix + "**"

    return updated_patterns


def components_for_wpt_paths(
    git_gecko: Repo, wpt_paths: Union[Set[str], Set[str]]
) -> Mapping[str, List[str]]:
    path_prefix = env.config["gecko"]["path"]["wpt"]
    paths = [os.path.join(path_prefix, item) for item in wpt_paths]

    mach = Mach(git_gecko.working_dir)
    output = mach.file_info("bugzilla-component", *paths)

    components: Mapping[str, List[str]] = defaultdict(list)
    current = None
    for line in output.split(b"\n"):
        if line.startswith(b" "):
            assert current is not None
            path = line.strip().decode("utf8", "replace")
            assert path.startswith(path_prefix)
            wpt_path = os.path.relpath(path, path_prefix)
            components[current].append(wpt_path)
        else:
            current = line.strip().decode("utf8")

    return components


def get(
    git_gecko: Repo,
    files_changed: Union[Set[str], Set[str]],
    default: Tuple[str, str],
) -> Tuple[str, str]:
    if not files_changed:
        return default

    components_dict = components_for_wpt_paths(git_gecko, files_changed)
    if not components_dict:
        return default

    components = sorted(list(components_dict.items()), key=lambda x: -len(x[1]))
    component = components[0][0]
    if component == "UNKNOWN" and len(components) > 1:
        component = components[1][0]

    if component == "UNKNOWN":
        return default

    product, component = component.split(" :: ", 1)
    return product, component


def mozbuild_path(repo_work: Repo) -> str:
    working_dir = repo_work.working_dir
    assert working_dir is not None
    return os.path.join(working_dir, env.config["gecko"]["path"]["wpt"], os.pardir, "moz.build")


def update(repo_work: Repo, renames: Dict[str, str]) -> None:
    mozbuild_file_path = mozbuild_path(repo_work)
    tests_base = os.path.split(env.config["gecko"]["path"]["wpt"])[1]

    def tests_rel_path(path: str) -> str:
        return os.path.join(tests_base, path)

    mozbuild_rel_renames = {
        tests_rel_path(old): tests_rel_path(new) for old, new in renames.items()
    }

    if os.path.exists(mozbuild_file_path):
        new_data = remove_obsolete(mozbuild_file_path, moves=mozbuild_rel_renames)
        with open(mozbuild_file_path, "w", encoding="utf8") as f:
            f.write(new_data)
    else:
        logger.warning("Can't find moz.build file to update")
