import requests
import time
from typing import Any, Iterable, Optional
from urllib import parse
from typing import Mapping

from .types import Json


WPT_FYI_BASE = "https://wpt.fyi/api/"
STAGING_HOST = "staging.wpt.fyi"


class Url:
    def __init__(self, initial_url: str):
        if initial_url:
            parts = parse.urlsplit(initial_url)
            self.scheme = parts.scheme
            self.host = parts.netloc
            self.path = parts.path
            self.query = parse.parse_qsl(parts.query, keep_blank_values=True)
            self.fragment = parts.fragment
        else:
            self.scheme = ""
            self.host = ""
            self.path = ""
            self.query = []
            self.fragment = ""

    def add_query(self, name: str, value: str) -> None:
        self.query.append((name, value))

    def build(self) -> str:
        return parse.urlunsplit((self.scheme,
                                 self.host,
                                 self.path,
                                 parse.urlencode(self.query),
                                 self.fragment))


def get_runs(sha: Optional[str] = None,
             pr: Optional[str] = None,
             max_count: Optional[str] = None,
             labels: Optional[list[str]] = None,
             staging: bool = False) -> list[Mapping[str, Json]]:
    url = Url(WPT_FYI_BASE + "runs")
    if staging:
        url.host = STAGING_HOST

    for name, value in [("sha", sha), ("pr", pr), ("max-count", max_count)]:
        if value is not None:
            url.add_query(name, value)
    if labels:
        for item in labels:
            url.add_query("label", item)

    resp = requests.get(url.build())
    resp.raise_for_status()
    return resp.json()


def get_results(run_ids: list[str],
                query: Optional[Json] = None,
                staging: bool = False) -> dict[str, Any]:
    url = Url(WPT_FYI_BASE + "search")
    if staging:
        url.host = STAGING_HOST

    body: dict[str, Json] = {
        "run_ids": run_ids
    }
    if query is not None:
        body["q"] = query

    # A 422 status means that the data isn't in the cache, so retry
    retry = 0
    timeout = 10.

    while retry < 5:
        resp = requests.post(url.build(), json=body)
        if resp.status_code != 422:
            break
        time.sleep(timeout)
        retry += 1
        timeout *= 1.5

    resp.raise_for_status()
    return resp.json()


def get_metadata(products: list[str],
                 link: Optional[Iterable[str]],
                 staging: bool = False) -> dict[str, Any]:
    url = Url(WPT_FYI_BASE + "metadata")
    if staging:
        url.host = STAGING_HOST

    if isinstance(products, str):
        url.add_query("product", products)
    else:
        for product in products:
            url.add_query("product", product)
    resp = requests.get(url.build())

    resp.raise_for_status()

    if link is not None:
        data = {}
        for test, values in resp.json().items():
            link_values = [item for item in values if link in item["url"]]
            if link_values:
                data[test] = link_values
    else:
        data = resp.json()

    return data
