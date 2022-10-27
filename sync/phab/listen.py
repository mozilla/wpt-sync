import time
import re
from phabricator import Phabricator
import newrelic.agent


from .. import log
from ..tasks import handle


logger = log.get_logger(__name__)

RE_EVENT = re.compile("[0-9]{5,}:")
RE_COMMIT = re.compile("(committed|accepted|added a reverting change for) r[A-Z]+[a-f0-9]+:")


class PhabEventListener:

    ignore_list = ["added inline comments to D",
                   "added a comment to D",
                   "added a reviewer for D",
                   "added reviewers for D",
                   "removed a reviewer for D",
                   "removed reviewers for D",
                   "requested review of D",
                   "requested changes to D",
                   "added a subscriber to D",
                   "added a project to D",
                   "edited reviewers for D",
                   "updated the summary of D",  # Maybe useful to upstream info?
                   "accepted D",  # Maybe useful to upstream info?
                   "retitled D",  # Maybe useful to upstream info?
                   "blocking reviewer(s) for D",
                   "planned changes to D",
                   "updated subscribers of D",
                   "resigned from D",
                   "changed the edit policy for D",
                   "removed a project from D",
                   "updated D",
                   "changed the visibility for D",
                   "updated the test plan for D"]

    event_mapping = {
        "updated the diff for D": "commit",
        "created D": "commit",
        "closed D": "closed",
        "abandoned D": "abandoned",
        "added a reverting change for D": None,  # Not sure what this is yet
        "reopened D": "commit",  # This may need its own event type
    }

    def __init__(self, config):
        self.running = True
        self.timer_in_seconds = config['phabricator']['listener']['interval']
        self.latest = None

        self.phab = Phabricator(host='https://phabricator.services.mozilla.com/api/',
                                token=config['phabricator']['token'])
        self.phab.update_interfaces()

    def run(self):
        # Run until told to stop.
        while self.running:
            feed = self.get_feed()
            self.parse(feed)
            time.sleep(self.timer_in_seconds)

    @newrelic.agent.background_task(name='feed-fetching', group='Phabricator')
    def get_feed(self, before=None):
        """ """
        if self.latest and before is None:
            before = int(self.latest['chronologicalKey'])

        feed = []

        def chrono_key(feed_story_tuple):
            return int(feed_story_tuple[1]["chronologicalKey"])

        # keep fetching stories from Phabricator until there are no more stories to fetch
        while True:
            result = self.phab.feed.query(before=before, view='text')
            if result.response:
                results = sorted(list(result.response.items()), key=chrono_key)
                results = list(map(self.map_feed_tuple, results))
                feed.extend(results)
                if len(results) == 100 and before is not None:
                    # There may be more events we wish to fetch
                    before = int(results[-1]["chronologicalKey"])
                    continue
            break
        return feed

    @newrelic.agent.background_task(name='feed-parsing', group='Phabricator')
    def parse(self, feed):
        # Go through rows in reverse order, and ignore first row as it has the table headers
        for event in feed:

            if RE_COMMIT.search(event['text']):
                # This is a commit event, ignore it
                continue

            # Split the text to get the part that describes the event type
            event_text = RE_EVENT.split(event['text'])[0]

            # Check if this is an event we wish to ignore
            if any(event_type in event_text for event_type in PhabEventListener.ignore_list):
                continue

            # Map the event text to an event type so we know how to handle it
            event['type'] = self.map_event_type(event_text, event)
            if event['type'] is None:
                continue

            # Add the event to the queue, and set this as the latest parsed
            handle.apply_async(("phabricator", event))
            self.latest = event

    @staticmethod
    def map_event_type(event_text, event):
        # Could use compiled regex expression instead
        for event_type, mapping in PhabEventListener.event_mapping.items():
            if event_type in event_text:
                return mapping

        logger.warning("Unknown phabricator event type: %s" % event_text)
        newrelic.agent.record_custom_event("unknown_phabricator_event", params={
            "event_text": event_text,
            "event": event,
        }, application=newrelic.agent.application())

    @staticmethod
    def map_feed_tuple(feed_tuple):
        story_phid, feed_story = feed_tuple
        feed_story.update({"storyPHID": story_phid})
        return feed_story


def run_phabricator_listener(config):
    logger.info("Starting Phabricator listener")
    listener = PhabEventListener(config)
    listener.run()


class MockPhabricator(Phabricator):

    def __init__(self, *args, **kwargs):
        self.feed = None
        pass

    def update_interfaces(self):
        pass
