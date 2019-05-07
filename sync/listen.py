import json
import logging
import os
import urlparse

import kombu

from kombu.mixins import ConsumerMixin

import log
import handlers
import tasks

here = os.path.dirname(__file__)

logger = log.get_logger(__name__)


def get_listen_logger(config):
    logger = logging.getLogger(__name__)

    log_dir = os.path.join(config["root"],
                           config["paths"]["logs"])
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    basic_formatter = logging.Formatter('[%(asctime)s] %(levelname)s:%(name)s:%(message)s')

    file_handler = logging.handlers.TimedRotatingFileHandler(os.path.join(log_dir, "listen.log"),
                                                             when="D", utc=True)
    file_handler.setFormatter(basic_formatter)

    logger.addHandler(file_handler)
    logger.propagate = False

    return logger


class Listener(ConsumerMixin):
    """Manages a single kombu.Consumer."""
    def __init__(self, conn, exchanges, queues, logger):
        self.connection = conn
        self._callbacks = {item: [] for item in exchanges}
        self._queues = queues
        self.connect_max_retries = 10
        self.logger = logger

    def get_consumers(self, Consumer, channel):
        consumer = Consumer(self._queues, callbacks=[self.on_message], auto_declare=False)
        return [consumer]

    def on_connection_revived(self):
        logger.debug("Connection to %s revived." % self.connection.hostname)

    def add_callback(self, exchange, func):
        if exchange is None:
            raise ValueError("Expected string, got None")
        self._callbacks[exchange].append(func)

    def on_message(self, body, message):
        exchange = message.delivery_info['exchange']
        callbacks = self._callbacks.get(exchange)
        try:
            if callbacks:
                for cb in callbacks:
                    cb(body)
            else:
                raise Exception('received message from unknown exchange: %s' %
                                exchange)
        finally:
            message.ack()


def get_listener(conn, userid, exchanges=None, extra_data=None, logger=None):
    """Obtain a Pulse consumer that can handle received messages.

    Returns a ``Listener`` instance bound to listen to the requested exchanges.
    Callers should use ``add_callback`` to register functions
    that will be called when a message is received.

    The callback functions receive one argument ``body``, the decoded message body.
    """
    queues = []

    if exchanges is None:
        raise ValueError("No exchanges supplied")

    for queue_name, exchange_name, key_name in exchanges:
        queue_name = 'queue/%s/%s' % (userid, queue_name)

        exchange = kombu.Exchange(exchange_name, type='topic',
                                  channel=conn)
        exchange.declare(passive=True)

        queue = kombu.Queue(name=queue_name,
                            exchange=exchange,
                            durable=True,
                            routing_key=key_name,
                            exclusive=False,
                            auto_delete=False,
                            channel=conn,
                            extra_data=extra_data)
        queues.append(queue)
        # queue.declare() declares the exchange, which isn't allowed by the
        # server. So call the low-level APIs to only declare the queue itself.
        queue.queue_declare()
        queue.queue_bind()

    return Listener(conn, [item[1] for item in exchanges], queues, logger)


def run_pulse_listener(config):
    """
    Configures Pulse connection and triggers events from Pulse messages.

    Connection details are managed at https://pulseguardian.mozilla.org/.
    """
    exchanges = [(config['pulse']['github']['queue'],
                  config['pulse']['github']['exchange'],
                  config['pulse']['github']['routing_key']),
                 (config['pulse']['hgmo']['queue'],
                  config['pulse']['hgmo']['exchange'],
                  config['pulse']['hgmo']['routing_key']),
                 (config['pulse']['taskcluster']['queue'],
                  config['pulse']['taskcluster']['exchange'],
                  config['pulse']['taskcluster']['routing_key']),
                 (config['pulse']['treeherder']['queue'],
                  config['pulse']['treeherder']['exchange'],
                  config['pulse']['treeherder']['routing_key']), ]

    conn = kombu.Connection(hostname=config['pulse']['host'],
                            port=config['pulse']['port'],
                            ssl=config['pulse']['ssl'],
                            userid=config['pulse']['username'],
                            password=config['pulse']['password'])

    listen_logger = get_listen_logger(config)

    with conn:
        try:
            listener = get_listener(conn,
                                    userid=config['pulse']['username'],
                                    exchanges=exchanges,
                                    logger=listen_logger)
            listener.add_callback(config['pulse']['github']['exchange'],
                                  GitHubFilter(config, listen_logger))
            listener.add_callback(config['pulse']['hgmo']['exchange'],
                                  PushFilter(config, listen_logger))
            listener.add_callback(config['pulse']['treeherder']['exchange'],
                                  TaskFilter(config, listen_logger))
            listener.add_callback(config['pulse']['taskcluster']['exchange'],
                                  TaskGroupFilter(config, listen_logger))
            listener.run()
        except KeyboardInterrupt:
            pass


class Filter(object):
    name = None
    task = tasks.handle

    def __init__(self, config, logger):
        self.config = config
        self.logger = logger

    def __call__(self, body):
        if self.accept(body):
            self.logger.info("Message accepted %s" % self.name)
            self.logger.debug(json.dumps(body))
            self.task.apply_async((self.name, body))

    def accept(self, body):
        raise NotImplementedError


class GitHubFilter(Filter):
    name = "github"
    event_filters = {item: lambda x: True for item in handlers.GitHubHandler.dispatch_event.keys()}
    event_filters["status"] = lambda x: x["payload"]["context"] != "upstream/gecko"
    event_filters["push"] = lambda x: x["payload"]["ref"] == "refs/heads/master"

    def __init__(self, config, logger):
        super(GitHubFilter, self).__init__(config, logger)
        repo_path = urlparse.urlparse(config["web-platform-tests"]["repo"]["url"]).path
        self.key_filter = "%s/" % repo_path.split("/", 2)[1]

    def accept(self, body):
        return (body["_meta"]["routing_key"].startswith(self.key_filter) and
                body["event"] in self.event_filters and
                self.event_filters[body["event"]](body))


class PushFilter(Filter):
    name = "push"

    def __init__(self, config, logger):
        super(PushFilter, self).__init__(config, logger)
        self.repos = set(config["gecko"]["repo"].keys())

    def accept(self, body):
        # Check that this has some commits pushed
        if not body["payload"].get("data", {}).get("pushlog_pushes"):
            return False

        repo = body["_meta"]["routing_key"]
        if "/" in repo:
            repo = repo.rsplit("/", 1)[1]
        return repo in self.repos


class TaskGroupFilter(Filter):
    name = "taskgroup"

    def accept(self, body):
        return body.get("taskGroupId")


class TaskFilter(Filter):
    name = "task"

    def accept(self, body):
        return (body["display"]["jobName"] == "Gecko Decision Task" and
                body["state"] == "completed")
