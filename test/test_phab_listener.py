from unittest import mock
from sync.phab.listen import PhabEventListener, MockPhabricator
from sync.gh import AttrDict


def test_parse_feed(env, phabricator_feed):
    events = []

    def mock_apply_async(event):
        events.append(event)

    with mock.patch("sync.phab.listen.Phabricator", MockPhabricator):
        listener = PhabEventListener(env.config)
        listener.phab.feed = AttrDict(
            {"query": mock.Mock(return_value=AttrDict({"response": phabricator_feed}))}
        )
        feed = listener.get_feed()
        assert len(feed) == 32
        with mock.patch("sync.tasks.handle.apply_async", mock_apply_async):
            listener.parse(feed)
            assert len(events) == 8
            assert events == [
                (
                    "phabricator",
                    {
                        "authorPHID": "PHID-USER-udrckg4snwyg3bct4zkg",
                        "objectPHID": "PHID-DREV-zgtbdg62bfte7trehmyk",
                        "text": (
                            "mixedpuppy abandoned D53870: Bug 1593236 support more than one "
                            "extension per listener for the activity logging api."
                        ),
                        "epoch": 1575482367,
                        "storyPHID": "PHID-STRY-aggopw42hur4q25gdso3",
                        "chronologicalKey": "6766645245666097994",
                        "type": "abandoned",
                        "class": "PhabricatorApplicationTransactionFeedStory",
                    },
                ),
                (
                    "phabricator",
                    {
                        "authorPHID": "PHID-USER-b3gypr5y2fwmvx5oxb6d",
                        "objectPHID": "PHID-DREV-wua46fflwex6wzs2p44x",
                        "text": (
                            "yzen created D55844: Bug 1599806 - remove setupInParent from "
                            "accessibility actor and split up child/parent functionality into "
                            "two separate actors. r=rcaliman,ochameau."
                        ),
                        "epoch": 1575482746,
                        "storyPHID": "PHID-STRY-pnednd5xkhzvw2mrrjvr",
                        "chronologicalKey": "6766646870533234513",
                        "type": "commit",
                        "class": "PhabricatorApplicationTransactionFeedStory",
                    },
                ),
                (
                    "phabricator",
                    {
                        "authorPHID": "PHID-USER-5zlrwolpbqtuwrk3umcz",
                        "objectPHID": "PHID-DREV-tlrcs7zxqldt4elbiy6x",
                        "text": (
                            "sfink updated the diff for D55715: Bug 1596943 - DEBUG-only "
                            "diagnostics for weakmap marking problems."
                        ),
                        "epoch": 1575482904,
                        "storyPHID": "PHID-STRY-2hthhwhjjhzec44hjyhn",
                        "chronologicalKey": "6766647551193873656",
                        "type": "commit",
                        "class": "PhabricatorApplicationTransactionFeedStory",
                    },
                ),
                (
                    "phabricator",
                    {
                        "authorPHID": "PHID-USER-erxup3oqd7if4an5l7ut",
                        "objectPHID": "PHID-DREV-xsq5e47odnjmtdbkrh4z",
                        "text": (
                            "mak updated the diff for D55310: Bug 1600244 - Don't store "
                            "favicons added after the initial page load. r=Mossop."
                        ),
                        "epoch": 1575483119,
                        "storyPHID": "PHID-STRY-eigmdjrdep63jkhn3ywv",
                        "chronologicalKey": "6766648475295573982",
                        "type": "commit",
                        "class": "PhabricatorApplicationTransactionFeedStory",
                    },
                ),
                (
                    "phabricator",
                    {
                        "authorPHID": "PHID-USER-tysl7a4scmhn7wsjdfa4",
                        "objectPHID": "PHID-DREV-3ckl2bcxvlkjl33222mg",
                        "text": (
                            "ato closed D55177: bug 1590828: remote: return NS exceptions from "
                            "nsIRemoteAgent."
                        ),
                        "epoch": 1575484597,
                        "storyPHID": "PHID-STRY-l6o4lfb4twfvk7kjbb56",
                        "chronologicalKey": "6766654822076720840",
                        "type": "closed",
                        "class": "PhabricatorApplicationTransactionFeedStory",
                    },
                ),
                (
                    "phabricator",
                    {
                        "authorPHID": "PHID-USER-uc4letlc2b6jaf3mbcdm",
                        "objectPHID": "PHID-DREV-bxdsrp4kuugsdtcgemjq",
                        "text": (
                            "apavel closed D54914: Bug 1596314 - disable "
                            "WebExecutorTest.readTimeout for frequent failures "
                            "r?#intermittent-reviewers."
                        ),
                        "epoch": 1575484604,
                        "storyPHID": "PHID-STRY-2znfb66iezsvn55jbeth",
                        "chronologicalKey": "6766654850459165001",
                        "type": "closed",
                        "class": "PhabricatorApplicationTransactionFeedStory",
                    },
                ),
                (
                    "phabricator",
                    {
                        "authorPHID": "PHID-USER-dxkrohpjet6yh65movaa",
                        "objectPHID": "PHID-DREV-t3sddx6ixb2knxroa3q5",
                        "text": (
                            "mattwoodrow closed D55568: Bug 1599662 - Add process "
                            "switching to the reftest harness so that we can get better "
                            "coverage for fission. r?dbaron,kmag."
                        ),
                        "epoch": 1575484862,
                        "storyPHID": "PHID-STRY-2b4vxk6rihlshhxzsuws",
                        "chronologicalKey": "6766655960944659677",
                        "type": "closed",
                        "class": "PhabricatorApplicationTransactionFeedStory",
                    },
                ),
                (
                    "phabricator",
                    {
                        "authorPHID": "PHID-USER-5zlrwolpbqtuwrk3umcz",
                        "objectPHID": "PHID-DREV-rbub2ah2hcxirfe3ouio",
                        "text": (
                            "sfink updated the diff for D55050: Bug 1597005 - Prevent "
                            "-Werror failure for bogus printf overflow warning in "
                            "ZydisAPI.cpp with gcc -O0."
                        ),
                        "epoch": 1575485698,
                        "storyPHID": "PHID-STRY-dfshbzzqn3psdstwgbqh",
                        "chronologicalKey": "6766659549788670587",
                        "type": "commit",
                        "class": "PhabricatorApplicationTransactionFeedStory",
                    },
                ),
            ]
