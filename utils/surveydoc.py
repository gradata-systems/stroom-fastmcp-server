"""The survey record: a Documentation doc per surveyed feed, readable by people and by survey_feed.

The doc shows which kinds of event the feed holds (counts, shares, streams, examples with where they are),
and ends with a fenced JSON block that survey_feed reads back to carry on where the last survey stopped:
which streams were read and which shapes are known. Example records are stored as they are; who can read
them is down to the permissions on the folder the doc lives in, as for the feed's data.
"""
import json
import re
from datetime import datetime, timezone
from typing import Any

STATE_FENCE = re.compile(r'```json survey-state\n(.*?)\n```', re.S)
MAX_EXAMPLE_CHARS = 4000


def doc_name(feed: str) -> str:
    return f'{feed} - Survey'


def read_state(markdown: str | None) -> dict[str, Any] | None:
    match = STATE_FENCE.search(markdown or '')
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except ValueError:
        return None


def merge(state: dict[str, Any] | None, result: dict[str, Any], shapes: dict[str, dict[str, Any]],
          examples_per_shape: int) -> dict[str, Any]:
    """Add one survey_feed call to the record: counts add up, examples are kept up to the limit."""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    state = state or {'feed': result['feed'], 'format': result.get('format'), 'streams_read': [], 'surveys': [],
                      'shapes': {}}
    state['format'] = state.get('format') or result.get('format')
    state['time_range'] = result.get('time_range') or state.get('time_range')
    state['streams_read'] = sorted(set(state['streams_read']) | set(result.get('streams_read') or []))
    state['saturated'] = bool(result.get('saturated'))
    state['surveys'].append({'at': now, 'streams': result.get('streams_read') or [],
                             'records': result.get('records_read', 0), 'new_shapes': result.get('new_shapes', 0)})
    for signature, shape in shapes.items():
        if not shape['count']:
            continue
        kept = state['shapes'].setdefault(signature, {'count': 0, 'streams': [], 'examples': [], 'first_seen': now})
        kept['count'] += shape['count']
        kept['streams'] = sorted(set(kept['streams']) | set(shape['streams']))[:50]
        texts = {e['text'] for e in kept['examples']}
        for example in shape['examples']:
            if len(kept['examples']) >= examples_per_shape:
                break
            if example['text'] not in texts:
                kept['examples'].append({'text': example['text'][:MAX_EXAMPLE_CHARS], 'location': example['location']})
    return state


def _cell(text: str, width: int = 120) -> str:
    text = re.sub(r'\s+', ' ', text).replace('|', '\\|')
    return text if len(text) <= width else text[:width - 1] + '…'


def render(state: dict[str, Any]) -> str:
    shapes = sorted(state['shapes'].items(), key=lambda kv: -kv[1]['count'])
    total = sum(s['count'] for _, s in shapes) or 1
    span = state.get('time_range') or {}
    last = state['surveys'][-1] if state['surveys'] else {}
    lines = [f"# {doc_name(state['feed'])}", '',
             f"Kinds of event found in feed **{state['feed']}** ({state.get('format')}): {len(shapes)} from "
             f"{sum(s['records'] for s in state['surveys'])} records in {len(state['streams_read'])} streams"
             + (f", created {span.get('oldest')} to {span.get('newest')}" if span else '') + '. '
             + ('The last survey found nothing new: the feed looks covered.' if state.get('saturated')
                else 'Not saturated yet: more streams may show more kinds of event.'), '',
             f"Last survey: {last.get('at')}. Examples are the feed's own records; where each one is (stream:part:record) "
             "is given so it can be stepped. Streams expire under retention, so older locations may be gone.", '',
             '## Kinds of event', '',
             '| # | Shape | Records | Share | Streams | Example |', '| --- | --- | --- | --- | --- | --- |']
    for n, (signature, shape) in enumerate(shapes, 1):
        example = shape['examples'][0]['text'] if shape['examples'] else ''
        lines.append(f"| {n} | {_cell(signature, 90)} | {shape['count']} | {round(100 * shape['count'] / total, 1)}% | "
                     f"{len(shape['streams'])} | {_cell(example, 100)} |")
    lines += ['', '## Examples', '']
    for n, (signature, shape) in enumerate(shapes, 1):
        lines.append(f"**{n}. {signature}**")
        lines.append('')
        for example in shape['examples']:
            loc = example['location']
            lines += [f"Stream {loc['stream']}, part {loc['part']}, record {loc['record']}:", '', '```',
                      example['text'], '```', '']
    lines += ['## Surveys', '', '| When | Streams read | Records | New kinds |', '| --- | --- | --- | --- |']
    lines += [f"| {s['at']} | {', '.join(map(str, s['streams']))} | {s['records']} | {s['new_shapes']} |"
              for s in state['surveys']]
    lines += ['', '## Survey state', '', 'Read by survey_feed to carry on where the last survey stopped; do not edit.', '',
              '```json survey-state', json.dumps(state, indent=1), '```', '']
    return '\n'.join(lines)
