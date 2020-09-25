from __future__ import absolute_import
import abc
import json
import logging
import os

import six
from six import iteritems, itervalues
from six.moves import urllib

import kombu
from kombu.mixins import ConsumerMixin

from . import log
from . import handlers
from . import tasks

MYPY = False
if MYPY:
    from typing import Any, Dict, Text

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
    # type: (Dict[Text, Any]) -> None
    """
    Configures Pulse connection and triggers events from Pulse messages.

    Connection details are managed at https://pulseguardian.mozilla.org/.
    """
    exchanges = []
    queues = {}
    for queue_name, queue_props in iteritems(config['pulse']):
        if (isinstance(queue_props, dict) and
            set(queue_props.keys()) == {"queue", "exchange", "routing_key"}):
            queues[queue_name] = queue_props

    for queue in itervalues(queues):
        logger.info("Connecting to pulse queue:%(queue)s exchange:%(exchange)s"
                    " route:%(routing_key)s" % queue)
        exchanges.append((queue['queue'],
                          queue['exchange'],
                          queue['routing_key']))

    conn = kombu.Connection(hostname=config['pulse']['host'],
                            port=config['pulse']['port'],
                            ssl=config['pulse']['ssl'],
                            userid=config['pulse']['username'],
                            password=config['pulse']['password'])

    listen_logger = get_listen_logger(config)

    filter_map = {
        'github': GitHubFilter,
        'hgmo': PushFilter,
        'taskcluster-taskgroup': TaskGroupFilter,
        'taskcluster-try-completed': DecisionTaskFilter,
        'taskcluster-try-failed': DecisionTaskFilter,
        'taskcluster-try-exception': DecisionTaskFilter,
        'taskcluster-wptsync-completed': TryTaskFilter,
        'taskcluster-wptsync-failed': TryTaskFilter,
        'taskcluster-wptsync-exception': TryTaskFilter,
    }

    with conn:
        try:
            listener = get_listener(conn,
                                    userid=config['pulse']['username'],
                                    exchanges=exchanges,
                                    logger=listen_logger)
            for queue_name, queue in iteritems(queues):
                queue_filter = filter_map[queue_name](config, listen_logger)
                listener.add_callback(queue['exchange'], queue_filter)

            listener.run()
        except KeyboardInterrupt:
            pass
    return None


class Filter(six.with_metaclass(abc.ABCMeta, object)):
    name = None  # type: Text
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
        # type: (Dict[Text, Any]) -> bool
        raise NotImplementedError


class GitHubFilter(Filter):
    name = "github"
    event_filters = {item: lambda x: True
                     for item in handlers.GitHubHandler.dispatch_event.keys()}
    event_filters["check_run"] = lambda x: x["payload"]["action"] == "completed"
    event_filters["push"] = lambda x: x["payload"]["ref"] == "refs/heads/master"

    def __init__(self, config, logger):
        super(GitHubFilter, self).__init__(config, logger)
        repo_path = urllib.parse.urlparse(config["web-platform-tests"]["repo"]["url"]).path
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
        return body.get("taskGroupId") is not None


class DecisionTaskFilter(Filter):
    name = "decision-task"

    def accept(self, body):
        return is_decision_task(body)


class TryTaskFilter(Filter):
    name = "try-task"

    def accept(self, body):
        return not is_decision_task(body)


def is_decision_task(body):
    tags = body.get("task", {}).get("tags", {})
    return (tags.get("kind") == "decision-task" and
            tags.get("createdForUser") == "wptsync@mozilla.com")
