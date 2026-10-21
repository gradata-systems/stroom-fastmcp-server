"""An action whose only fit is Data (Network for a DNS lookup): one way to write it, and the refusals say that way.

Seen in testing: a Fortigate rule wrote EventDetail/Network/Data/Source/...; the schema problem suggested
EventDetail/Network/Data/Data (a Data inside Network's Data), and the rule then sent as fields [] with a data list was
refused for having no action element, with no word on how to name one that is Data itself."""
import json

import pytest
from pydantic import ValidationError

from tests.test_xsltgen import BASE, RECORDS, SCHEMA, VALIDATOR, mapping, transform
from utils.xsltgen import TranslationMapping, generate

NS = {'e': 'event-logging:3'}


def network_data(**rule) -> TranslationMapping:
    return mapping(events=[{'name': 'dns', 'fields': [{'path': 'EventDetail/TypeId', 'value': 'DNS'}], **rule}])


def test_a_network_event_of_data_alone_is_written_as_networks_data():
    m = network_data(fields=[{'path': 'EventDetail/TypeId', 'value': 'DNS'},
                             {'path': 'EventDetail/Network/Data', 'data_name': 'action', 'field': 'action'}],
                     data=['sid'])
    result = generate(m, SCHEMA, '4.1.0')
    assert result['ok'], result['problems']
    events = transform(result['xslt'], RECORDS)
    assert VALIDATOR.validate(events), [e.message for e in VALIDATOR.error_log]
    names = [d.get('Name') for d in events.find('e:Event', NS).findall('e:EventDetail/e:Network/e:Data', NS)]
    assert names == ['action', 'sid']


def test_a_value_below_networks_data_is_suggested_as_that_data_not_a_data_inside_it():
    m = network_data(fields=[{'path': 'EventDetail/TypeId', 'value': 'DNS'},
                             {'path': 'EventDetail/Network/Data', 'data_name': 'action', 'field': 'action'},
                             {'path': 'EventDetail/Network/Data/Source/Device/IPAddress', 'field': 'host'}])
    problems = generate(m, SCHEMA, '4.1.0')['problems']
    [problem] = [p for p in problems if 'Network/Data/Source' in p]
    suggested = json.loads(problem.split('carry it as Data of the action element instead: ')[1].split(' (Name and')[0])
    assert suggested == {'path': 'EventDetail/Network/Data', 'data_name': 'host', 'field': 'host'}


def test_a_data_list_with_no_action_element_says_how_to_name_one_that_is_data():
    with pytest.raises(ValidationError) as e:
        network_data(fields=[], data=['action', 'sid'])
    said = str(e.value)
    assert "[dns] data needs the rule's action element, which none of its fields names" in said
    assert '{"path": "EventDetail/Network/Data", "data_name": "action", "field": "action"}' in said
