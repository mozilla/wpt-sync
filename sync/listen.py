import os

import log
import settings
import socket
import urlparse

import kombu
from mozvcssync import pulse

import update
import downstream

here = os.path.dirname(__file__)

logger = log.get_logger("listen")


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
                    cb(body, message, self._extra_data)
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


class OnGitHubCallback(object):
    def __init__(self, config):
        self.config = config

    def __call__(self, body, message, config):
        routing_key = body['_meta']['routing_key']
        if not routing_key.startswith("w3c/"):
            return

        logger.debug('Look, an event:' + body['event'])
        logger.debug('from:' + body['_meta']['routing_key'])

        if body['event'] == "pull_request":
            # TODO, check if we created this PR
            downstream.downstream(body)

        # TODO: other events to check if we can merge a PR
        # because of some update


class OnCommitCallback(object):
    def __init__(self, config):
        self.config = config

        self.integration_repos = {}
        for repo_name, url in config["sync"]["integration"].iteritems():
            url_parts = urlparse.urlparse(url)
            url = urlparse.urlunparse(("https",) + url_parts[1:])
            self.integration_repos[url] = repo_name

        self.landing_repo = config["sync"]["landing"]

    def __call__(self, body, message, config):
        data = body["payload"]["data"]
        repo_url = data["repo_url"]
        logger.debug("Commit landed in repo %s" % repo_url)
        if repo_url in self.integration_repos:
            update.integration_commit(self.integration_repos[repo_url])
        elif repo_url == self.landing_repo:
            update.landing_commit(self.integration_repos[repo_url])


def on_task_finished(body, message, config):
    # TODO: check if this is a try job we started, and if so
    # update the metadata for the relevant branch, or land the commits
    logger.debug("Taskcluster task group finished %s" % body)


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
                  config['pulse']['taskcluster']['routing_key']),]

    consumer = get_consumer(userid=config['pulse']['username'],
                            password=config['pulse']['password'],
                            hostname=config['pulse']['host'],
                            port=config['pulse']['port'],
                            ssl=config['pulse']['ssl'],
                            exchanges=exchanges)

    consumer.add_callback(config['pulse']['github']['exchange'], OnGitHubCallback(config))
    consumer.add_callback(config['pulse']['hgmo']['exchange'], OnCommitCallback(config))
    consumer.add_callback(config['pulse']['taskcluster']['exchange'], on_task_finished)

    try:
        with consumer:
            consumer.listen_forever()
    except KeyboardInterrupt:
        pass


@settings.configure
def main(config):
    run_pulse_listener(config)


if __name__ == "__main__":
    main()
