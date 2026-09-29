"""What promotion checks: a clean step of the pipeline's current code (remembered from stepping), and docs."""
from types import SimpleNamespace

from tools import builds, stepping

OWN = {'type': 'Pipeline', 'uuid': 'p', 'name': 'Acme'}
TEMPLATE = {'type': 'Pipeline', 'uuid': 't', 'name': 'Event Data (Text)'}
LAYERS = [
    # The template sets the text converter; the pipeline sets its own XSLT.
    {'sourcePipeline': TEMPLATE, 'pipelineData': {
        'elements': {'add': [{'id': 'dsParser', 'type': 'DSParser'}, {'id': 'translationFilter', 'type': 'XSLTFilter'}]},
        'properties': {'add': [{'element': 'dsParser', 'name': 'textConverter',
                                'value': {'entity': {'type': 'TextConverter', 'uuid': 'tc', 'name': 'shared'}}}]}}},
    {'sourcePipeline': OWN, 'pipelineData': {'properties': {'add': [
        {'element': 'translationFilter', 'name': 'xslt', 'value': {'entity': {'type': 'XSLT', 'uuid': 'x', 'name': 'Acme'}}}]}}},
]


class FakeStroom:
    def __init__(self):
        self.code = {'x': '<xsl:stylesheet>v1</xsl:stylesheet>', 'tc': 'template code'}

    async def pipeline_layers(self, uuid):
        return LAYERS

    async def get_doc(self, doc_type, uuid):
        return {'data': self.code[uuid]}


def ctx(stroom):
    return SimpleNamespace(lifespan_context={'stroom': stroom})


async def test_only_the_pipelines_own_code_is_fingerprinted():
    prints = await stepping.code_fingerprint(FakeStroom(), 'p')
    assert list(prints) == ['translationFilter']


async def test_a_clean_draft_counts_once_it_is_saved_and_a_later_edit_does_not():
    stroom = FakeStroom()
    c = ctx(stroom)
    assert not await stepping.stepped_clean(c, 'p')
    draft = {'translationFilter': '<xsl:stylesheet>v2</xsl:stylesheet>'}
    await stepping.remember_clean(c, 'p', draft, {'verdict': 'clean', 'records_stepped': 5})
    assert not await stepping.stepped_clean(c, 'p')  # the saved code is still v1
    stroom.code['x'] = draft['translationFilter']
    assert await stepping.stepped_clean(c, 'p')
    stroom.code['x'] = '<xsl:stylesheet>v3</xsl:stylesheet>'
    assert not await stepping.stepped_clean(c, 'p')


async def test_blocking_or_empty_runs_are_not_remembered():
    c = ctx(FakeStroom())
    await stepping.remember_clean(c, 'p', None, {'verdict': 'blocking', 'records_stepped': 5})
    await stepping.remember_clean(c, 'p', None, {'verdict': 'clean', 'records_stepped': 0})
    assert not await stepping.stepped_clean(c, 'p')


async def test_build_checks_name_what_is_missing():
    c = ctx(FakeStroom())
    await stepping.remember_clean(c, 'p', None, {'verdict': 'clean', 'records_stepped': 3})
    docs = [{'type': 'Pipeline', 'uuid': 'p', 'name': 'Acme', 'working_copy_of': None},
            {'type': 'Pipeline', 'uuid': 'q', 'name': 'Other', 'working_copy_of': None},
            {'type': 'Documentation', 'uuid': 'd', 'name': 'Other', 'working_copy_of': None}]
    problems = await builds.build_checks(c, docs)
    assert problems == [
        "Pipeline 'Acme': no documentation (write_documentation)",
        "Pipeline 'Other': no clean step_sample or step_records of its current code is recorded on this server",
    ]
