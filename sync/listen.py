import os
import urlparse

from mozillapulse.consumers import GenericConsumer, PulseConfiguration
from update import update

import log
import settings

here = os.path.dirname(__file__)

logger = log.get_logger("listen")


def read_credentials():
    with open(os.path.join(here, "credentials")) as f:
        username = f.readline().strip()
        password = f.readline().strip()
    return username, password


class PushConsumer(GenericConsumer):
    def __init__(self, **kwargs):
        super(PushConsumer, self).__init__(
            PulseConfiguration(**kwargs),
            ['exchange/hgpushes/v1',
             ], **kwargs)


class UpdateCallback(object):
    def __init__(self, config):
        self.config = config

        self.integration_repos = {}
        for repo_name, url in config["sync"]["integration"].iteritems():
            url_parts = urlparse.urlparse(url)
            url = urlparse.urlunparse(("https",) + url_parts[1:])
            self.integration_repos[url] = repo_name

        landing_url = config["sync"]["landing"]
        landing_name = landing_url.rsplit("/", 1)[1]
        self.landing_repo = {landing_url: landing_name}

    def __call__(self, body, msg):
        logger.debug("Got pulse message %s" % body)
        data = body["payload"]
        print data["repo_url"], data["repo_url"] in self.integration_repos
        if data["repo_url"] in self.integration_repos:
            update(self.integration_repos[data["repo_url"]])
        #TODO: handle landings


def start_consumer(username, password, callback):
    c = PushConsumer(user=username,
                     password=password,
                     topic='#',
                     callback=callback)
    c.listen()


@settings.configure
def main(config):
    start_consumer(config["pulse"]["username"], config["pulse"]["password"], UpdateCallback(config))


if __name__ == "__main__":
    main()
