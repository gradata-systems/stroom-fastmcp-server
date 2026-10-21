"""The sample check of build_translation_xslt: off the event loop, fast on a large key=value sample, and bounded.

Seen in production: a Fortigate sample (10 streams of 1 MB, 60 extract entries drafted from its key=value body) was
checked for over a minute on the event loop; the health probe went unanswered and the pod was restarted mid-call."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests.test_xsltgen import LOGON, SCHEMA
from tools import generation
from utils import xpathcheck
from utils.localcheck import sample_records
from utils.saxon import processor
from utils.xpathcheck import check_extractions, python_twin
from utils.xsltgen import TranslationMapping, literal

KEYS = ['logid', 'type', 'action', 'srcip', 'dstip', 'user', 'msg']


def body(i: int) -> str:
    pairs = {'logid': f'"{i:010d}"', 'type': '"traffic"' if i % 3 else '"event"', 'action': '"accept"',
             'srcip': f'10.0.0.{i % 250}', 'dstip': '8.8.8.8', 'user': f'"u{i}"', 'msg': f'"message {i}"'}
    if i % 7 == 0:
        pairs.pop('user')       # a key some records lack: a warning, matches some
    return ' '.join(f'{k}={v}' for k, v in pairs.items())


SAMPLE = '\n'.join(json.dumps({'time': '2026-10-01T10:00:00.000Z', 'host': 'fw1', 'body': body(i)}) for i in range(400))


def kv_mapping(extra: list | None = None) -> TranslationMapping:
    extract = [{'field': 'body', 'regex': f'(?:^|[\\s,;]){k}="([^"]*)"' if k in ('logid', 'type', 'action', 'user', 'msg')
                else f'(?:^|[\\s,;]){k}=([^\\s,;"]*)', 'names': [k]} for k in KEYS]
    return TranslationMapping.model_validate({
        'input': 'json', 'json_layout': 'lines', 'extract': extract + (extra or []),
        'common': [{'path': 'EventTime/TimeCreated', 'field': 'time'},
                   {'path': 'EventSource/System/Name', 'value': 'FortiOS'},
                   {'path': 'EventSource/System/Environment', 'value': 'Test'},
                   {'path': 'EventSource/Generator', 'value': 'fortigate'},
                   {'path': 'EventSource/Device/HostName', 'field': 'host'},
                   {'path': 'EventSource/User/Id', 'field': 'user'}],
        'events': [{'name': 'logon', 'fields': LOGON}]})


def checked(m: TranslationMapping, **kw):
    return check_extractions(m, SAMPLE, None, sample_records(m, SAMPLE, None)[0], **kw)


def saxon_matches(regex: str, flags: str, text: str) -> bool:
    xp = processor().new_xpath_processor()
    return xp.effective_boolean_value(f"matches({literal(text)}, {literal(regex)}, {literal(flags)})")


@pytest.mark.parametrize('regex, flags, text', [
    ('a\\sb', '', 'a b'),      # no-break space: \s to Python, not to XPath
    ('a\\sb', '', 'a\fb'),          # form feed, likewise
    ('a\\sb', '', 'a\tb'),
    ('a\\Sb', '', 'a b'),
    ('a.b', '', 'a\rb'),            # . is not a carriage return in XPath
    ('a.b', 's', 'a\nb'),
    ('abc$', '', 'abc\n'),          # $ before a last newline: Python's, not XPath's
    ('abc$', 'm', 'abc\nx'),
    ('^x=([^\\s,;"]*)', '', 'x=1 2'),
    ('[\\s,;]k="([^"]*)"', '', 'a k="v"'),
    ('[\\]a]+', '', ']a'),
    ('[^\\]a]', '', 'b'),
    ('ABC', 'i', 'abc'),
    ('(\\d+)-(\\d+)', '', '12-34'),
])
def test_the_python_twin_reads_an_xpath_regex_as_saxon_does(regex, flags, text):
    twin = python_twin(regex, flags)
    assert twin is not None
    assert bool(twin.search(text)) == saxon_matches(regex, flags, text), (regex, text)


@pytest.mark.parametrize('regex, flags', [
    ('\\w+', ''), ('\\p{L}+', ''), ('[a-z-[aeiou]]', ''), ('a b', 'x'), ('\\i\\c*', ''), ('[\\S]', ''), ('a', 'q')])
def test_no_twin_where_python_reads_it_otherwise(regex, flags):
    # \w in XPath is every character but punctuation, separators and others: '=' is one, as Python's is not.
    assert python_twin(regex, flags) is None


def test_the_check_with_the_twin_says_what_saxon_alone_says():
    m = kv_mapping([{'field': 'body', 'regex': '(?:^|[\\s,;])absent=(\\S*)', 'names': ['absent']},
                    {'field': 'body', 'regex': '\\w+=(\\w+)', 'names': ['first']}])      # no twin: Saxon's
    fast = checked(m)
    with patch.object(xpathcheck, 'python_twin', lambda regex, flags: None):
        slow = checked(m)
    assert fast == slow
    problems, warnings = fast
    assert len(problems) == 1 and problems[0].startswith('extract[7] (body): the regex matches none of the 400')
    assert len(warnings) == 1 and warnings[0].startswith('extract[5] (body): the regex matches 342 of the 400')


def test_a_twin_that_disagrees_with_saxon_is_not_trusted():
    import re
    m = kv_mapping()
    with patch.object(xpathcheck, 'python_twin', lambda regex, flags: re.compile('')):    # matches everything
        problems, warnings = checked(m)
    # Saxon's count stands: the user key is missing from every seventh record.
    assert not problems and warnings[0].startswith('extract[5] (body): the regex matches 342 of the 400')


def test_entries_past_the_deadline_are_named_not_checked():
    m = kv_mapping([{'field': 'body', 'regex': '(?:^|[\\s,;])absent=(\\S*)', 'names': ['absent']}])
    problems, warnings = checked(m, deadline=time.monotonic() - 1)
    assert not problems
    assert warnings == [f"{', '.join(f'extract[{n}] (body)' for n in range(8))} not checked against the sample: the "
                        f"check's time ran out (the server's sample_check_seconds). step_sample shows what they give."]


async def test_the_sample_check_runs_beside_the_event_loop():
    seen = {}
    real = generation.check_extractions

    def slow(*args, **kw):
        seen['thread'] = threading.current_thread() is not threading.main_thread()
        time.sleep(0.4)
        return real(*args, **kw)

    ticks = []

    async def ticker():
        while 'done' not in seen:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.05)

    ctx = SimpleNamespace(lifespan_context={'stroom': SimpleNamespace(settings=SimpleNamespace(sample_check_seconds=60))})
    with patch.object(generation, 'event_schema', AsyncMock(return_value=SCHEMA)), \
            patch.object(generation, 'applicable_instructions', AsyncMock(return_value={'instructions': []})), \
            patch.object(generation, 'check_extractions', slow):
        async def call():
            try:
                return await generation.build_translation_xslt(ctx, kv_mapping(), sample=SAMPLE)
            finally:
                seen['done'] = True
        result, _ = await asyncio.gather(call(), ticker())
    assert result['ok'] and seen['thread']
    # The loop went on while the check ran: a health probe is answered, other users' calls go on.
    assert len(ticks) >= 5


def test_a_large_key_value_sample_is_checked_in_seconds():
    sample = '\n'.join(json.dumps({'time': '2026-10-01T10:00:00.000Z', 'host': 'fw1',
                                   'body': body(i) + ' ' + ' '.join(f'k{j}="v{j}"' for j in range(50))})
                       for i in range(3000))
    m = kv_mapping([{'field': 'body', 'regex': f'(?:^|[\\s,;])k{j}="([^"]*)"', 'names': [f'k{j}']} for j in range(50)])
    started = time.monotonic()
    problems, _ = check_extractions(m, sample, None, sample_records(m, sample, None)[0])
    assert not problems and time.monotonic() - started < 20


async def test_concurrent_checks_share_saxon_safely():
    # Two users' calls check at once, each in its own worker thread, through the server's one Saxon processor.
    m = kv_mapping([{'field': 'body', 'regex': '\w+=(\w+)', 'names': ['first']}])     # Saxon over every text
    alone = checked(m)
    together = await asyncio.gather(*(asyncio.to_thread(checked, m) for _ in range(6)))
    assert all(r == alone for r in together)


def test_saxon_is_never_started_on_a_worker_thread():
    # Started on a worker thread, Saxon crashed the process (a fatal StackOverflowError in its isolate): it starts on
    # the main thread, when utils.saxon is imported, and a worker finding none is refused instead.
    from concurrent.futures import ThreadPoolExecutor
    from utils import saxon
    assert saxon._processor is not None
    with patch.object(saxon, '_processor', None), ThreadPoolExecutor(1) as pool:
        with pytest.raises(RuntimeError, match='started on the main thread'):
            pool.submit(saxon.processor).result()
