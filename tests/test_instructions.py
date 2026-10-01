from types import SimpleNamespace

from config import Settings
from tools import instructions

SETTINGS = Settings(_env_file=None, stroom_url='https://s', dev_no_auth=True, stroom_api_key='k')
DOCS = {'g': ('System', 'Always map the acting user to EventSource/User/Id.'),
        'e': ('System / Feeds / Events', 'Event feeds: TypeId is the vendor event code.'),
        'k': ('System / Feeds / Events / Keycloak', 'Keycloak: realm goes in EventSource/System/Environment.'),
        'x': ('System / Elastic Indices', 'Index names are ecs-<source>-v<n>.')}
FEEDS = {'KEYCLOAK-V1.2': 'System / Feeds / Events / Keycloak'}


class FakeStroom:
    settings = SETTINGS

    async def find_documents(self, name, types, limit):
        if types == ['Documentation']:
            values = [{'docRef': {'type': 'Documentation', 'uuid': u, 'name': 'AGENTS'}, 'path': path}
                      for u, (path, _) in DOCS.items()]
            values.append({'docRef': {'type': 'Documentation', 'uuid': 'n', 'name': 'AGENTS notes'}, 'path': 'System'})
            return {'values': values}
        return {'values': [{'docRef': {'type': 'Feed', 'uuid': 'f', 'name': name}, 'path': FEEDS[name]}] if name in FEEDS else []}

    async def get_doc(self, doc_type, uuid):
        return {'data': DOCS[uuid][1]}  # the body, as typed in the Stroom UI


def ctx():
    return SimpleNamespace(lifespan_context={'stroom': FakeStroom()})


def test_scope_is_the_folder_and_below_and_root_docs_apply_everywhere():
    assert instructions.applies(['System'], ['Anything'])
    assert instructions.applies(['System', 'Feeds', 'Events'], ['System', 'Feeds', 'Events', 'Keycloak'])
    assert not instructions.applies(['System', 'Elastic Indices'], ['System', 'Feeds', 'Events'])


async def test_instructions_for_a_feed_run_from_general_to_specific():
    result = await instructions.get_instructions(ctx(), feeds=['KEYCLOAK-V1.2'])
    assert [i['folder'] for i in result['instructions']] == ['System', 'System/Feeds/Events', 'System/Feeds/Events/Keycloak']
    assert result['instructions'][0]['applies_to'] == 'everything'
    assert result['instructions'][2]['instructions'].startswith('Keycloak: realm')
    assert result['other_instruction_docs'] == [{'folder': 'System/Elastic Indices', 'uuid': 'x'}]


async def test_without_targets_only_root_docs_apply_and_names_must_match_exactly():
    result = await instructions.get_instructions(ctx())
    assert [i['uuid'] for i in result['instructions']] == ['g']
    assert {d['uuid'] for d in result['other_instruction_docs']} == {'e', 'k', 'x'}


async def test_folders_can_be_given_directly():
    result = await instructions.get_instructions(ctx(), folders=['System/Elastic Indices/Keycloak'])
    assert [i['uuid'] for i in result['instructions']] == ['g', 'x']
