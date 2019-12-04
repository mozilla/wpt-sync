import mock
from sync.phab.listen import PhabEventListener, MockPhabricator
from sync.gh import AttrDict


def test_parse_feed(env, phabricator_feed):
    events = []

    def mock_apply_async(event):
        events.append(event)

    with mock.patch("sync.phab.listen.Phabricator", MockPhabricator):
        listener = PhabEventListener(env.config)
        listener.phab.feed = AttrDict({
            'query': mock.Mock(return_value=AttrDict({'response': phabricator_feed}))
        })
        feed = listener.get_feed()
        assert len(feed) == 30
        with mock.patch("sync.tasks.handle.apply_async", mock_apply_async):
            listener.parse(feed)
            assert len(events) == 8
            assert '\n'.join(map(str, events)) == """('phabricator', {u'authorPHID': u'PHID-USER-udrckg4snwyg3bct4zkg', u'objectPHID': u'PHID-DREV-zgtbdg62bfte7trehmyk', u'text': u'mixedpuppy abandoned D53870: Bug 1593236 support more than one extension per listener for the activity logging api.', u'epoch': 1575482367, 'storyPHID': u'PHID-STRY-aggopw42hur4q25gdso3', u'chronologicalKey': u'6766645245666097994', 'type': 'abandoned', u'class': u'PhabricatorApplicationTransactionFeedStory'})
('phabricator', {u'authorPHID': u'PHID-USER-b3gypr5y2fwmvx5oxb6d', u'objectPHID': u'PHID-DREV-wua46fflwex6wzs2p44x', u'text': u'yzen created D55844: Bug 1599806 - remove setupInParent from accessibility actor and split up child/parent functionality into two separate actors. r=rcaliman,ochameau.', u'epoch': 1575482746, 'storyPHID': u'PHID-STRY-pnednd5xkhzvw2mrrjvr', u'chronologicalKey': u'6766646870533234513', 'type': 'commit', u'class': u'PhabricatorApplicationTransactionFeedStory'})
('phabricator', {u'authorPHID': u'PHID-USER-5zlrwolpbqtuwrk3umcz', u'objectPHID': u'PHID-DREV-tlrcs7zxqldt4elbiy6x', u'text': u'sfink updated the diff for D55715: Bug 1596943 - DEBUG-only diagnostics for weakmap marking problems.', u'epoch': 1575482904, 'storyPHID': u'PHID-STRY-2hthhwhjjhzec44hjyhn', u'chronologicalKey': u'6766647551193873656', 'type': 'commit', u'class': u'PhabricatorApplicationTransactionFeedStory'})
('phabricator', {u'authorPHID': u'PHID-USER-erxup3oqd7if4an5l7ut', u'objectPHID': u'PHID-DREV-xsq5e47odnjmtdbkrh4z', u'text': u"mak updated the diff for D55310: Bug 1600244 - Don't store favicons added after the initial page load. r=Mossop.", u'epoch': 1575483119, 'storyPHID': u'PHID-STRY-eigmdjrdep63jkhn3ywv', u'chronologicalKey': u'6766648475295573982', 'type': 'commit', u'class': u'PhabricatorApplicationTransactionFeedStory'})
('phabricator', {u'authorPHID': u'PHID-USER-tysl7a4scmhn7wsjdfa4', u'objectPHID': u'PHID-DREV-3ckl2bcxvlkjl33222mg', u'text': u'ato closed D55177: bug 1590828: remote: return NS exceptions from nsIRemoteAgent.', u'epoch': 1575484597, 'storyPHID': u'PHID-STRY-l6o4lfb4twfvk7kjbb56', u'chronologicalKey': u'6766654822076720840', 'type': 'closed', u'class': u'PhabricatorApplicationTransactionFeedStory'})
('phabricator', {u'authorPHID': u'PHID-USER-uc4letlc2b6jaf3mbcdm', u'objectPHID': u'PHID-DREV-bxdsrp4kuugsdtcgemjq', u'text': u'apavel closed D54914: Bug 1596314 - disable WebExecutorTest.readTimeout for frequent failures r?#intermittent-reviewers.', u'epoch': 1575484604, 'storyPHID': u'PHID-STRY-2znfb66iezsvn55jbeth', u'chronologicalKey': u'6766654850459165001', 'type': 'closed', u'class': u'PhabricatorApplicationTransactionFeedStory'})
('phabricator', {u'authorPHID': u'PHID-USER-dxkrohpjet6yh65movaa', u'objectPHID': u'PHID-DREV-t3sddx6ixb2knxroa3q5', u'text': u'mattwoodrow closed D55568: Bug 1599662 - Add process switching to the reftest harness so that we can get better coverage for fission. r?dbaron,kmag.', u'epoch': 1575484862, 'storyPHID': u'PHID-STRY-2b4vxk6rihlshhxzsuws', u'chronologicalKey': u'6766655960944659677', 'type': 'closed', u'class': u'PhabricatorApplicationTransactionFeedStory'})
('phabricator', {u'authorPHID': u'PHID-USER-5zlrwolpbqtuwrk3umcz', u'objectPHID': u'PHID-DREV-rbub2ah2hcxirfe3ouio', u'text': u'sfink updated the diff for D55050: Bug 1597005 - Prevent -Werror failure for bogus printf overflow warning in ZydisAPI.cpp with gcc -O0.', u'epoch': 1575485698, 'storyPHID': u'PHID-STRY-dfshbzzqn3psdstwgbqh', u'chronologicalKey': u'6766659549788670587', 'type': 'commit', u'class': u'PhabricatorApplicationTransactionFeedStory'})"""  # noqa E501
