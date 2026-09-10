import time
from urllib.parse import urljoin
from typing import Any, Mapping, Optional

import requests


git2hg_cache: dict[str, str] = {}


class Lando:
    def __init__(self, config: Mapping[str, Any]):
        self.base_url = config["lando"]["url"]
        self.api_try_token = config["lando"]["try_api_token"]

    def request(
        self,
        method: str,
        path: str,
        retry_count: int = 1,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[Mapping[str, Any]] = None,
    ) -> Optional[Mapping[str, Any]]:
        exc = None
        for _retry_count in range(retry_count):
            resp = requests.request(
                method, urljoin(self.base_url, path), headers=headers, json=body
            )
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

    def try_push(
        self,
        patches: list[str],
        base_commit: str,
    ) -> int:
        body = {
            "base_commit": base_commit,
            "base_commit_vcs": "hg",
            "patch_format": "git-format-patch",
            "patches": patches,
            "repo_name": "try",
        }
        headers = {"Authorization": f"Bearer {self.api_try_token}"}
        data = self.request("POST", "/api/try/patches", headers=headers, body=body)
        if data is None or not isinstance(data.get("id"), int):
            raise ValueError(f"Lando response missing id property: {data}")

        return data["id"]

    def landing_job(self, job_id: int) -> Mapping[str, Any]:
        """Get the current state of a Lando landing job"""
        data = self.request("GET", f"/landing_jobs/{job_id}", retry_count=5)
        if data is None:
            raise ValueError(f"Lando has no job with id {job_id}")

        return data

    def hg2git(self, hg_hash: str) -> Optional[str]:
        data = self.request("GET", f"/api/hg2git/firefox/{hg_hash}", retry_count=5)
        if data is None:
            return None
        if not isinstance(data.get("git_hash"), str):
            raise ValueError(f"Lando response missing git_hash property: {data}")

        return data["git_hash"]

    def git2hg(self, git_hash: str) -> Optional[str]:
        if git_hash not in git2hg_cache:
            data = self.request("GET", f"/api/git2hg/firefox/{git_hash}", retry_count=5)
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
        self.try_pushes: list[Mapping[str, Any]] = []
        self.job_status: dict[int, str] = {}
        self.default_job_status = "LANDED"

    def try_push(
        self,
        patches: list[str],
        base_commit: str,
    ) -> int:
        self.try_pushes.append(
            {
                "base_commit": base_commit,
                "base_commit_vcs": "hg",
                "patch_format": "git-format-patch",
                "patches": patches,
                "repo_name": "try",
            }
        )
        return len(self.try_pushes)

    def landing_job(self, job_id: int) -> Mapping[str, Any]:
        if not 0 < job_id <= len(self.try_pushes):
            raise ValueError(f"Lando has no job with id {job_id}")
        status = self.job_status.get(job_id, self.default_job_status)
        return {
            "id": job_id,
            "status": status,
            "commit_id": "%040x" % job_id if status == "LANDED" else None,
            "error": "",
            "url": urljoin(self.base_url, f"/landings/{job_id}"),
        }

    def hg2git(self, hg_hash: str) -> Optional[str]:
        return self.hg_to_git.get(hg_hash)

    def git2hg(self, git_hash: str) -> Optional[str]:
        return self.git_to_hg.get(git_hash)
