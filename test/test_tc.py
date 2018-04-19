import requests_mock

from sync import tc


def test_taskgroup(tc_response):
    with tc_response("taskgroup-wpt-complete.json") as f:
        with requests_mock.Mocker() as m:
            taskgroup_id = "test"
            m.register_uri("GET", "%stask-group/%s/list" % (tc.QUEUE_BASE, taskgroup_id), body=f)
            taskgroup = tc.TaskGroup(taskgroup_id)
            taskgroup.refresh()

    assert len(taskgroup.tasks) == 20
    tasks = taskgroup.view()
    assert tasks
    assert len(tasks) == 20
    assert not list(tasks.incomplete_tasks())
    assert tasks.is_complete(allow_unscheduled=True)

    wpt_tasks = taskgroup.view(tc.is_suite_fn("web-platform-tests"))
    assert len(wpt_tasks) == 7

    builds = tasks.filter(tc.is_build)
    assert len(builds) == 8

    failures = tasks.filter(tc.is_status_fn(tc.FAIL))
    assert len(failures) == 1


def test_taskgroup_unscheduled(tc_response):
    with tc_response("taskgroup-build-failed.json") as f:
        with requests_mock.Mocker() as m:
            taskgroup_id = "test"
            m.register_uri("GET", "%stask-group/%s/list" % (tc.QUEUE_BASE, taskgroup_id), body=f)
            taskgroup = tc.TaskGroup(taskgroup_id)
            taskgroup.refresh()

    tasks = taskgroup.view()
    assert not tasks.is_complete()
    assert tasks.is_complete(allow_unscheduled=True)

    assert len(taskgroup.view(lambda x: tc.is_build(x) and tc.is_status(tc.FAIL, x))) == 10

    wpt_tasks = taskgroup.view(tc.is_suite_fn("web-platform-tests"))
    assert wpt_tasks.is_complete(allow_unscheduled=True)
    assert not wpt_tasks.is_complete()
