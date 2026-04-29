import time
from urllib.parse import urljoin
from typing import Any, Mapping, Optional

import requests


git2hg_cache: dict[str, str] = {}


class Lando:
    def __init__(self, config: Mapping[str, Any]):
        self.base_url = config["lando"]["api_url"]

    def get(self, path: str) -> Optional[Mapping[str, Any]]:
        exc = None
        for _retry_count in range(5):
            resp = requests.get(urljoin(self.base_url, path))
            if resp.status_code == 404:
                return None
            try:
                resp.raise_for_status()
            except Exception as e:
                exc = e
                if resp.status_code == 502:
                    # retry
                    time.sleep(1)
                    continue
                raise
            data = resp.json()
            if not isinstance(data, dict):
                raise ValueError(f"Expected map, got {data}")
            return data
        assert exc is not None
        raise exc

    def hg2git(self, hg_hash: str) -> Optional[str]:
        data = self.get(f"/hg2git/firefox/{hg_hash}")
        if data is None:
            return None
        if not isinstance(data.get("git_hash"), str):
            raise ValueError(f"Lando response missing git_hash property: {data}")

        return data["git_hash"]

    def git2hg(self, git_hash: str) -> Optional[str]:
        if git_hash not in git2hg_cache:
            data = self.get(f"/git2hg/firefox/{git_hash}")
            if data is None:
                return None
            if not isinstance(data.get("hg_hash"), str):
                raise ValueError(f"Lando response missing hg_hash property: {data}")

            git2hg_cache[git_hash] = data["hg_hash"]

        return git2hg_cache[git_hash]


class MockLando(Lando):
    def __init__(self, config: Mapping[str, Any]):
        super().__init__(config)
        self.hg_to_git: dict[str, Optional[str]] = {}
        self.git_to_hg: dict[str, Optional[str]] = {}

    def hg2git(self, hg_hash: str) -> Optional[str]:
        return self.hg_to_git.get(hg_hash)

    def git2hg(self, git_hash: str) -> Optional[str]:
        return self.git_to_hg.get(git_hash)
