import json
from sync import listen


def test_try_task_filter(env, tc_response):
    filter_ = listen.TryTaskFilter(env.config, listen.logger)
    with tc_response("decision-task-success-pulse.json") as f:
        data = json.load(f)
        assert filter_.accept(data) is False

    with tc_response("test-task-success-pulse.json") as f:
        data = json.load(f)
        assert filter_.accept(data) is True


def test_decision_task_filter(env, tc_response):
    filter_ = listen.DecisionTaskFilter(env.config, listen.logger)
    with tc_response("decision-task-success-pulse.json") as f:
        data = json.load(f)
        assert filter_.accept(data) is True

    with tc_response("test-task-success-pulse.json") as f:
        data = json.load(f)
        assert filter_.accept(data) is False
