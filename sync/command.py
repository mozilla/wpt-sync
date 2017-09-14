import argparse

import log
import model
import push
import settings
from tasks import setup

logger = log.get_logger("command")


def get_parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    parser.add_argument("--pre-set-state", action="store",
                        help="Set the state of a state object (Landing or Sync) to the given value before running the task")
    parser.add_argument("--set-state", action="store",
                        help="Set the state of a state object (Landing or Sync) to the given value after running the task")

    parser_landing = subparsers.add_parser("landing", help="Trigger the landing code")
    parser_landing.set_defaults(func=landing)
    return parser


@settings.configure
def landing(config, *args, **kwargs):
    session, git_gecko, git_wpt, gh_wpt, bz = setup()

    landing = model.Landing.current(session)
    if landing is None:
        logger.error("No landing in progress")
        return

    with model.session_scope(session):
        if "pre_set_state" in kwargs:
            landing.state = getattr(model.LandingState, kwargs["pre_set_state"])

    #TODO add some locking so there isn't another task in progress when running this?
    push.land_to_gecko(config, session, git_gecko, git_wpt, gh_wpt, bz, landing.head_commit.rev)

    with model.session_scope(session):
        if "set_state" in kwargs:
            landing.state = getattr(model.LandingState, kwargs["set_state"])


def main():
    parser = get_parser()
    args = parser.parse_args()
    args.func(vars(args))


if __name__ == "__main__":
    main()
