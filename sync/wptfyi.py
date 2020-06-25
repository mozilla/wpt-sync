from __future__ import absolute_import
import requests
import time
import six.moves.urllib.request, six.moves.urllib.parse, six.moves.urllib.error
import six.moves.urllib.parse

from six import iteritems
import six


WPT_FYI_BASE = "https://wpt.fyi/api/"
STAGING_HOST = "staging.wpt.fyi"


class Url(object):
    def __init__(self, initial_url):
        if initial_url:
            parts = six.moves.urllib.parse.urlsplit(initial_url)
            self.scheme = parts.scheme
            self.host = parts.netloc
            self.path = parts.path
            self.query = six.moves.urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            self.fragment = parts.fragment
        else:
            self.scheme = ""
            self.host = ""
            self.path = ""
            self.query = []
            self.fragment = ""

    def scheme(self, value):
        self.scheme = value
        return self

    def host(self, value):
        self.host = value
        return self

    def path(self, value):
        self.path = value
        return self

    def add_path(self, value):
        self.path = six.moves.urllib.parse.urljoin(self.path, value)
        return self

    def query(self, value):
        self.query = value
        return self

    def add_query(self, name, value):
        self.query.append((name, value))
        return self

    def fragment(self, value):
        self.fragment = value
        return self

    def build(self):
        return six.moves.urllib.parse.urlunsplit((self.scheme,
                                    self.host,
                                    self.path,
                                    six.moves.urllib.parse.urlencode(self.query),
                                    self.fragment))


def get_runs(sha=None, pr=None, max_count=None, labels=None, staging=False):
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


def get_results(run_ids, test=None, query=None, staging=False):
    url = Url(WPT_FYI_BASE + "search")
    if staging:
        url.host = STAGING_HOST

    body = {
        "run_ids": run_ids
    }
    if query is not None:
        body["q"] = query

    # A 422 status means that the data isn't in the cache, so retry
    retry = 0
    timeout = 10

    while retry < 5:
        resp = requests.post(url.build(), json=body)
        if resp.status_code != 422:
            break
        time.sleep(timeout)
        retry += 1
        timeout *= 1.5

    resp.raise_for_status()
    return resp.json()


def get_metadata(products, link, staging=False):
    url = Url(WPT_FYI_BASE + "metadata")
    if staging:
        url.host = STAGING_HOST

    if isinstance(products, six.string_types):
        url.add_query("product", products)
    else:
        for product in products:
            url.add_query("product", product)
    resp = requests.get(url.build())

    resp.raise_for_status()

    if link is not None:
        data = {}
        for test, values in iteritems(resp.json()):
            link_values = [item for item in values if link in item["url"]]
            if link_values:
                data[test] = link_values
    else:
        data = resp.json()

    return data
