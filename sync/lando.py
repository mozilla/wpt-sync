import csv
import json
import os
import urllib.request

from .env import Environment

env = Environment()

_git_to_hg: dict[str, str] | None = None
_hg_to_git: dict[str, str] | None = None


def hg2git(hg_hash: str) -> str:
    _, hg_to_git = _load_csv()
    if hg_hash in hg_to_git:
        return hg_to_git[hg_hash]
    response = urllib.request.urlopen(env.config["lando"]["api_url"] + "/hg2git/firefox/" + hg_hash)  # nosec B310
    data = response.read()
    map = json.loads(data.decode("utf-8"))
    assert isinstance(map, dict)
    assert isinstance(map["git_hash"], str)

    _save_new_hashes(map["git_hash"], hg_hash)

    return map["git_hash"]


def git2hg(git_hash: str) -> str:
    git_to_hg, _ = _load_csv()
    if git_hash in git_to_hg:
        return git_to_hg[git_hash]
    response = urllib.request.urlopen(
        env.config["lando"]["api_url"] + "/git2hg/firefox/" + git_hash
    )  # nosec B310
    data = response.read()
    map = json.loads(data.decode("utf-8"))
    assert isinstance(map, dict)
    assert isinstance(map["hg_hash"], str)

    _save_new_hashes(git_hash, map["hg_hash"])

    return map["hg_hash"]


def _load_csv() -> tuple[dict[str, str], dict[str, str]]:
    global _git_to_hg, _hg_to_git
    if _git_to_hg is None or _hg_to_git is None:
        git_to_hg: dict[str, str] = {}
        hg_to_git: dict[str, str] = {}

        csv_path = os.path.join(env.config["root"], "config", "prod", "git2hg.csv")
        with open(csv_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                git_to_hg[row["git"]] = row["hg"]
                hg_to_git[row["hg"]] = row["git"]

        _git_to_hg = git_to_hg
        _hg_to_git = hg_to_git
    return _git_to_hg, _hg_to_git


def _save_new_hashes(git_hash: str, hg_hash: str) -> None:
    git_to_hg, hg_to_git = _load_csv()

    git_to_hg[git_hash] = hg_hash
    hg_to_git[hg_hash] = git_hash

    csv_path = os.path.join(env.config["root"], "config", "prod", "git2hg.csv")
    with open(csv_path, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([git_hash, hg_hash])
