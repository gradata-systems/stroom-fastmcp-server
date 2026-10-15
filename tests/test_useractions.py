"""Steps that are the user's, as entries a client shows as buttons (asked for by the user, for Stroom's Ask Stroom AI)."""
from types import SimpleNamespace

import pytest

from utils import useractions

SETTINGS = SimpleNamespace(stroom_ui_url='https://stroom.example', stroom_url='http://stroom:8080')
PIPELINE = {'uuid': 'p-1', 'name': 'Acme-Events'}


def test_an_enable_button_names_the_filter_and_its_pipeline():
    action = useractions.enable_filter(SETTINGS, 9, PIPELINE, 'processes feed ACME (Raw Events)', ['drop-index'])
    assert action == {'id': 'enable-9', 'action': 'enable_processor_filter', 'filter_id': 9,
                      'doc': {'type': 'Pipeline', 'uuid': 'p-1', 'name': 'Acme-Events'},
                      'link': 'https://stroom.example/?action=open-doc&docType=Pipeline&docUuid=p-1',
                      'summary': "Enable filter 9 on pipeline 'Acme-Events': processes feed ACME (Raw Events)",
                      'after': ['drop-index']}
    assert useractions.disable_filter(SETTINGS, 4, PIPELINE, 'the earlier version', optional=True)['optional'] is True
    opened = useractions.open_document(SETTINGS, {'type': 'Feed', 'uuid': 'f-1', 'name': 'ACME'}, 'upload samples')
    assert opened['action'] == 'open_document' and opened['link'].endswith('docType=Feed&docUuid=f-1')


@pytest.mark.parametrize('request_, offered', [
    ('PUT _index_template/ecs-acme-v1\n{"index_patterns": ["ecs-acme-v1*"]}', True),
    ('DELETE _data_stream/ecs-acme-v1', True),
    ('DELETE ecs-acme-v1', True),
    # Never a button: a wildcard, every index, several, or any other request.
    ('DELETE ecs-*', False),
    ('DELETE _all', False),
    ('DELETE ecs-acme-v1,ecs-other-v1', False),
    ('POST ecs-acme-v1/_delete_by_query {"query": {"match_all": {}}}', False),
    ('PUT _cluster/settings\n{}', False),
])
def test_only_a_template_commit_or_one_index_dropped_is_offered(request_, offered):
    action = useractions.es_request('x', request_, 'why')
    assert (action is not None) is offered
    if offered:
        assert action['by'] == 'cluster admin' and action['request'] == request_


def test_left_out_entries_are_dropped():
    assert useractions.actions(None, None) == {}
    assert useractions.actions(useractions.es_request('x', 'DELETE _all', 'why'),
                               useractions.enable_filter(SETTINGS, 1, PIPELINE))['user_actions'][0]['id'] == 'enable-1'
