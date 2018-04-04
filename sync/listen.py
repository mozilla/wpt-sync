import os
import socket
import urlparse

import kombu

import log
import handlers
import tasks
import tc

here = os.path.dirname(__file__)

logger = log.get_logger(__name__)


class Consumer(object):
    """Represents a Pulse consumer to version control data."""
    def __init__(self, conn, exchanges, extra_data):
        self._conn = conn
        self._consumer = None
        self._entered = False
        self._callbacks = {item: [] for item in exchanges}
        self._extra_data = extra_data

    def __enter__(self):
        self._consumer.consume()
        self._entered = True
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self._consumer.cancel()
        self._entered = False

    def add_callback(self, exchange, func):
        if exchange is not None:
            self._callbacks[exchange].append(func)
        else:
            for callbacks in self.callbacks.itervalues():
                callbacks.append(func)

    def drain_events(self, timeout=0.1):
        """Drain all active events and call callbacks."""
        if not self._entered:
            raise Exception('must enter context manager before calling')

        try:
            self._conn.drain_events(timeout=timeout)
        except socket.timeout:
            pass

    def listen_forever(self):
        """Listen for and handle messages until interrupted."""
        if not self._entered:
            raise Exception('must enter context manager before calling')

        while True:
            try:
                self._conn.drain_events(timeout=1.0)
            except socket.timeout:
                pass

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


def get_consumer(userid, password,
                 hostname='pulse.mozilla.org',
                 port=5571,
                 ssl=True,
                 exchanges=None,
                 extra_data=None):
    """Obtain a Pulse consumer that can handle received messages.

    Caller passes Pulse connection details, including credentials. These
    credentials are managed at https://pulseguardian.mozilla.org/.

    Returns a ``Consumer`` instance bound to listen to the requested exchanges.
    Callers should append functions to the ``github_callbacks`` and/or
    ``hgmo_callbacks`` lists of this instance to register functions that will
    be called when a message is received.

    The returned ``Consumer`` must be active as a context manager for processing
    to work.

    The callback functions receive arguments ``body``, ``message``,
    and ``extra_data``. ``body`` is the decoded message body. ``message`` is
    the AMQP message from Pulse.  ``extra_data`` holds optional data for the
    consumers.

     **Callbacks must call ``message.ack()`` to acknowledge the message when
     done processing it.**
    """
    conn = kombu.Connection(
        hostname=hostname,
        port=port,
        ssl=ssl,
        userid=userid,
        password=password)
    conn.connect()

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

    consumer = Consumer(conn, [item[1] for item in exchanges], extra_data)
    kombu_consumer = conn.Consumer(queues, callbacks=[consumer.on_message],
                                   auto_declare=False)
    consumer._consumer = kombu_consumer

    # queue.declare() declares the exchange, which isn't allowed by the
    # server. So call the low-level APIs to only declare the queue itself.
    for queue in kombu_consumer.queues:
        queue.queue_declare()
        queue.queue_bind()

    return consumer


def run_pulse_listener(config):
    """Trigger events from Pulse messages."""
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

    consumer = get_consumer(userid=config['pulse']['username'],
                            password=config['pulse']['password'],
                            hostname=config['pulse']['host'],
                            port=config['pulse']['port'],
                            ssl=config['pulse']['ssl'],
                            exchanges=exchanges)

    consumer.add_callback(config['pulse']['github']['exchange'],
                          GitHubFilter(config))
    consumer.add_callback(config['pulse']['hgmo']['exchange'],
                          PushFilter(config))
    consumer.add_callback(config['pulse']['treeherder']['exchange'],
                          TaskFilter(config))
    consumer.add_callback(config['pulse']['taskcluster']['exchange'],
                          TaskGroupFilter(config))

    try:
        with consumer:
            consumer.listen_forever()
    except KeyboardInterrupt:
        pass


class Filter(object):
    name = None
    task = tasks.handle

    def __init__(self, config):
        self.config = config

    def __call__(self, body):
        if self.accept(body):
            self.task.apply_async((self.name, body))

    def accept(self, body):
        raise NotImplementedError


class GitHubFilter(Filter):
    name = "github"
    event_filters = {item: lambda x: True for item in handlers.GitHubHandler.dispatch_event.keys()}
    event_filters["status"] = lambda x: x["payload"]["context"] != "upstream/gecko"
    event_filters["push"] = lambda x: x["payload"]["ref"] == "refs/heads/master"

    def __init__(self, config):
        self.config = config
        repo_path = urlparse.urlparse(config["web-platform-tests"]["repo"]["url"]).path
        self.key_filter = "%s/" % repo_path.split("/", 2)[1]

    def accept(self, body):
        return (body["_meta"]["routing_key"].startswith(self.key_filter) and
                body["event"] in self.event_filters and
                self.event_filters[body["event"]](body))


class PushFilter(Filter):
    name = "push"

    def __init__(self, config):
        self.config = config
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
                body["state"] == tc.SUCCESS)
