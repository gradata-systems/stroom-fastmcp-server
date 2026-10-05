"""Network events indexed whichever action they record: one field for a source address, not one per Permit and Deny."""
from pathlib import Path

import yaml

from utils.fieldplan import FieldPlan, PlannedField, any_action, population_of, source_matches
from utils.templatecheck import names_from_example

CONVENTIONS = Path(__file__).parent.parent / 'conventions'
# A user's example unrelated to the source (it was a Twitter index's), with one field named IpAddress.
EXAMPLE = {'Id': {'type': 'long'}, 'Text': {'type': 'text'}, 'IpAddress': {'type': 'ip'},
           'User.Id': {'type': 'keyword'}, 'TypeId': {'type': 'keyword'}}
POPULATED = {'EventTime/TimeCreated': 100.0, 'EventSource/Device/HostName': 100.0, 'EventSource/User/Id': 10.0,
             'EventDetail/TypeId': 100.0, 'EventDetail/Authenticate/Action': 10.0,
             'EventDetail/Network/Permit/Source/Device/IPAddress': 40.0, 'EventDetail/Network/Permit/Source/Port': 40.0,
             'EventDetail/Network/Permit/Destination/Device/IPAddress': 40.0,
             'EventDetail/Network/Deny/Source/Device/IPAddress': 30.0, 'EventDetail/Network/Deny/Source/Port': 30.0,
             'EventDetail/Network/Deny/Destination/Device/IPAddress': 30.0, 'EventDetail/Network/Deny/Data': 30.0}


def test_a_wildcard_source_names_every_network_action():
    source = 'EventDetail/Network/*/Source/Device/IPAddress'
    assert source_matches(source, 'EventDetail/Network/Deny/Source/Device/IPAddress')
    assert not source_matches(source, 'EventDetail/Network/Deny/Destination/Device/IPAddress')
    assert population_of(source, POPULATED) == 70.0          # Permit's 40% and Deny's 30%: an event has one action
    assert any_action('EventDetail/Network/Deny/Data') == 'EventDetail/Network/*/Data'
    assert any_action('EventDetail/Authenticate/Action') == 'EventDetail/Authenticate/Action'


def test_the_plan_takes_network_fields_once_nested_as_the_example_nests():
    # Seen: the plan took IpAddress from the Deny destination only (the first path ending IPAddress), permitted
    # traffic's addresses went unindexed, and the agent added SourceIp.Permit, SourceIp.Deny and the like.
    profiles = {p.stem: yaml.safe_load(p.read_text(encoding='utf-8')) for p in CONVENTIONS.glob('*.yaml')}
    fields = [PlannedField(name='StreamId', type='id', source='@StreamId')]
    for path, spec in profiles['ecs']['field_map'].items():
        if population_of(path, POPULATED) and not any(f.name == spec['name'] for f in fields):
            fields.append(PlannedField(name=spec['name'], type=spec['type'], source=path))
    known: dict[str, set[str]] = {}
    for profile in profiles.values():
        for path, spec in profile['field_map'].items():
            known.setdefault(path, set()).add(spec['name'])
    named, _ = names_from_example([f.model_dump() for f in fields], EXAMPLE, known, sorted(POPULATED))
    by_source = {f['source']: f['name'] for f in named}
    assert by_source['EventDetail/Network/*/Source/Device/IPAddress'] == 'Source.IPAddress'
    assert by_source['EventDetail/Network/*/Destination/Device/IPAddress'] == 'Destination.IPAddress'
    assert by_source['EventDetail/Network/*/Source/Port'] == 'Source.Port'
    assert not any('/Permit/' in s or '/Deny/' in s for s in by_source)          # no field per action
    assert 'IpAddress' not in by_source.values()       # which address the example's name means can't be told
    xslt = FieldPlan(backend='elasticsearch', index_name='fw', time_field='@timestamp',
                     fields=[PlannedField(**f) for f in named]).xslt()
    assert '<xsl:if test="EventDetail/Network/*/Source/Device/IPAddress">' in xslt
