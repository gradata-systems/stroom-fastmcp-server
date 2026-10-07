"""Evaluate a mapping's xpath inputs against the sample, before anything is created or stepped.

An xpath that selects nothing in any sample record (json-to-xml(event)/timestamp where the JSON parser gives
string[@key='event'], say) generates valid XSLT that steps clean and writes empty elements or matches no rule.
Each record is rebuilt the way the pipeline's parser presents it to the XSLT (the JSONParser's map/string
elements, the Data Splitter's records:2 data elements, or the XML record itself) and every xpath is evaluated on
it with Saxon, as the translation would. Expressions that need Stroom (stroom: functions) or variables are not
evaluated, nor are for_each mappings, whose xpaths read items.
"""
import json
import re
import unicodedata
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from lxml import etree

from utils.dsgen import SplitterSpec, dry_run
from utils.localcheck import sample_records
from utils.xsltgen import TranslationMapping, input_namespace

_processor = None


def _saxon():
    global _processor
    if _processor is None:
        # A runtime dependency: it was a dev one, and build_translation_xslt failed in a deployed image.
        from saxonche import PySaxonProcessor
        _processor = PySaxonProcessor(license=False)
    return _processor


def json_xml(value: Any, key: str | None = None) -> str:
    """A JSON value as the JSONParser writes it: map, array, string, number, boolean and null elements, keyed."""
    k = f' key={quoteattr(key)}' if key is not None else ''
    if isinstance(value, dict):
        return f'<map{k}>' + ''.join(json_xml(v, name) for name, v in value.items()) + '</map>'
    if isinstance(value, list):
        return f'<array{k}>' + ''.join(json_xml(v) for v in value) + '</array>'
    if isinstance(value, bool):
        return f'<boolean{k}>{"true" if value else "false"}</boolean>'
    if value is None:
        return f'<null{k}/>'
    if isinstance(value, (int, float)):
        return f'<number{k}>{value}</number>'
    return f'<string{k}>{escape(str(value))}</string>'


def _json_values(text: str) -> list[dict[str, Any]]:
    text = text.strip('﻿\r\n ')
    try:
        if text.startswith('['):
            return [v for v in json.loads(text) if isinstance(v, dict)]
        return [v for v in (json.loads(line) for line in text.splitlines() if line.strip()) if isinstance(v, dict)]
    except ValueError:
        return []


def record_documents(mapping: TranslationMapping, sample: str | list[str], splitter: SplitterSpec | None) -> list[str]:
    """Each sample record as the XML document the translation reads it from (its root element is the record)."""
    texts = sample if isinstance(sample, list) else [sample]
    ns = input_namespace(mapping)
    if mapping.input == 'json':
        return [json_xml(v).replace('<map', f'<map xmlns="{ns}"', 1) for t in texts for v in _json_values(t)]
    if mapping.input == 'data_splitter':
        if splitter is None:
            return []
        return [f'<record xmlns="{ns}">' + ''.join(f'<data name={quoteattr(n)} value={quoteattr(str(v))}/>'
                                                   for n, v in r.items()) + '</record>'
                for t in texts for r in dry_run(splitter, t.strip('﻿\r\n '))['records']]
    records, _ = sample_records(mapping, sample, splitter)
    elements = [r for r in records if isinstance(r, etree._Element)]
    if mapping.input == 'xml_fragments':
        # As the XMLFragmentParser's wrapper gives them: a fragment declaring no namespace takes its default. Read
        # without it, the checks passed xpaths that select nothing in Stroom.
        wrapper = input_namespace(mapping)
        elements = [etree.fromstring(f'<records xmlns="{wrapper}">'.encode() + etree.tostring(r) + b'</records>')[0]
                    for r in elements]
    return [etree.tostring(r, encoding='unicode') for r in elements]


def xpath_inputs(mapping: TranslationMapping) -> dict[str, list[str]]:
    """Each xpath the mapping reads, with what it is used for."""
    used: dict[str, list[str]] = {}
    for rule_name, entries in [(None, mapping.common)] + [(r.name, r.fields) for r in mapping.events]:
        for e in entries:
            for x in (e.xpath, e.lookup.xpath if e.lookup else None):
                if x:
                    used.setdefault(x, []).append(e.path if rule_name is None else f'[{rule_name}] {e.path}')
    for rule in mapping.events:
        for c in rule.when:
            if c.xpath:
                used.setdefault(c.xpath, []).append(f'[{rule.name}] when')
    for d in mapping.drop_when:
        for c in d.when:
            if c.xpath:
                used.setdefault(c.xpath, []).append(f'drop: {d.reason}')
    for ex in mapping.extract:
        if ex.xpath:
            used.setdefault(ex.xpath, []).append('extract')
    # Not those calling a shared XSLT's functions: only Stroom, which imports it, can run them.
    imported = [f'{f.prefix}:' for f in mapping.functions]
    return {x: u for x, u in used.items() if 'stroom:' not in x and '$' not in x and not any(p in x for p in imported)}


# Characters a regex written from memory gets wrong: what the text has, and what the regex was likely given.
CONFUSABLE = {'\u2013': '-', '\u2014': '-', '\u2212': '-', '\u2010': '-', '\u00a0': ' ', '\u2009': ' ',
              '\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"', '\t': ' '}
ESCAPED_TWICE = (" `\\\\` in a regex matches a backslash character. A literal [ or ( is `\\[` or `\\(`: one backslash "
                 "in the regex itself, which your call's JSON writes as two; nothing else escapes it again.")
_PY_FLAGS = {'i': re.IGNORECASE, 'm': re.MULTILINE, 's': re.DOTALL, 'x': re.VERBOSE}


def _shown(text: str, limit: int = 60) -> str:
    """Text as it is, in backticks, for a message: never repr(), which doubles each backslash (and the reply's JSON
    doubles it again), and an agent then 'fixes' the escaping of a regex that was right."""
    text = text if len(text) <= limit else text[:limit] + '…'
    return '`' + text.replace('`', "'") + '`'


def where_it_stops(regex: str, flags: str, texts: list[str]) -> str:
    """Where a regex stops matching the texts: the longest start of it that matches the start of a text, and what
    the text has there instead of what the regex goes on with."""
    py_flags = 0
    for f in flags:
        py_flags |= _PY_FLAGS.get(f, 0)
    best = None   # (matched characters, cut, text)
    for text in texts[:8]:
        for cut in range(len(regex), 0, -1):
            try:
                found = re.match(regex[:cut], text, py_flags)
            except re.error:
                continue
            if found:
                if best is None or found.end() > best[0] or (found.end() == best[0] and cut > best[1]):
                    best = (found.end(), cut, text)
                break
    if best is None:
        return f"Not even its start matches: the text starts {_shown(texts[0])}."
    end, cut, text = best
    rest, there = regex[cut:], text[end:]
    if not rest.strip('$'):
        return (f"All of it matches, but the text goes on after: {_shown(there)}; end the regex with what follows, "
                f"or drop the $.")
    message = (f"It matches as far as {_shown(text[max(0, end - 40):end])} and stops there: the regex goes on with "
               f"{_shown(rest, 30)}, the text with {_shown(there, 30)}.")
    if there[:1] in CONFUSABLE:
        ch = there[0]
        message += (f" The text has {unicodedata.name(ch, 'U+%04X' % ord(ch))} (U+{ord(ch):04X}), not "
                    f"{_shown(CONFUSABLE[ch])}: put that character in the regex as it is (or a class with both).")
    if '\\\\' in rest[:6]:
        message += ESCAPED_TWICE
    return message


def check_extractions(mapping: TranslationMapping, sample: str | list[str], splitter: SplitterSpec | None,
                      records: list[Any]) -> tuple[list[str], list[str]]:
    """Each extraction's regex run (with XPath's own regex rules, by Saxon) on the text it reads in the sample:
    one that matches none is a problem, as its fields would be empty in every event; one that matches some, a
    warning. Seen: a regex written with '-' where the text has an en dash matched nothing, nothing said so, and the
    agent changed the escaping a hundred times."""
    if mapping.for_each or not mapping.extract:
        return [], []
    from utils.localcheck import _value_of
    from utils.xsltgen import literal
    proc = _saxon()
    ns = input_namespace(mapping)
    documents = None
    produced = set()
    problems, warnings = [], []
    for n, ex in enumerate(mapping.extract):
        source = ex.field or ex.xpath
        where = f"extract[{n}] ({source})"
        names = [x for x in ex.names if x]
        if ex.field in produced:
            produced.update(names)
            continue    # it reads a field an earlier extraction makes: checked through that one
        produced.update(names)
        texts: list[str] = []
        if ex.field and mapping.input in ('data_splitter', 'json'):
            texts = [str(v) for v in (_value_of(r, ex.field) for r in records if isinstance(r, dict)) if v not in (None, '')]
        else:
            if documents is None:
                documents = [proc.parse_xml(xml_text=d) for d in record_documents(mapping, sample, splitter)[:200]]
            for doc in documents:
                xp = proc.new_xpath_processor()
                if ns:
                    xp.declare_namespace('', ns)
                xp.set_context(xdm_item=doc)
                try:
                    value = xp.evaluate_single(f'string((/*/({source}))[1])')
                except Exception:
                    continue
                text = value.string_value if value is not None else ''
                if text:
                    texts.append(text)
        texts = texts[:200]
        if not texts:
            continue    # nothing to read: the xpath and field checks say so
        xp = proc.new_xpath_processor()
        try:
            hits = xp.evaluate_single(f"count(({', '.join(literal(t) for t in texts)})"
                                      f"[matches(., {literal(ex.regex)}, {literal(ex.flags)})])")
            matched = int(hits.string_value) if hits is not None else 0
        except Exception as e:
            problems.append(f"{where}: the regex is not a valid XPath regular expression: "
                            f"{str(e).strip().splitlines()[0][:200]}." + (ESCAPED_TWICE if '\\\\' in ex.regex else ''))
            continue
        if matched == 0:
            misses = [t for t in texts]
            problems.append(f"{where}: the regex matches none of the {len(texts)} sample texts, so {names} would be empty "
                            f"in every event. {where_it_stops(ex.regex, ex.flags, misses)}")
        elif matched < len(texts):
            missed = next((t for t in texts if not proc.new_xpath_processor().effective_boolean_value(
                f"matches({literal(t)}, {literal(ex.regex)}, {literal(ex.flags)})")), None)
            warnings.append(f"{where}: the regex matches {matched} of the {len(texts)} sample texts; the rest leave "
                            f"{names} empty. E.g. {_shown(missed or '', 160)}: "
                            + (where_it_stops(ex.regex, ex.flags, [missed]) if missed else ''))
    return problems, warnings


def check_xpaths(mapping: TranslationMapping, sample: str | list[str], splitter: SplitterSpec | None = None) -> list[str]:
    """Warnings for xpaths that select nothing in any sample record (or fail to evaluate)."""
    if mapping.for_each:
        return []
    inputs = xpath_inputs(mapping)
    if not inputs:
        return []
    documents = record_documents(mapping, sample, splitter)
    if not documents:
        return []
    proc = _saxon()
    ns = input_namespace(mapping)
    parsed = [proc.parse_xml(xml_text=d) for d in documents]
    warnings = []
    for x, used in inputs.items():
        found, error = False, None
        for doc in parsed:
            xp = proc.new_xpath_processor()
            if ns:
                xp.declare_namespace('', ns)
            xp.set_context(xdm_item=doc)
            try:
                if xp.effective_boolean_value(f'exists((/*/({x}))[normalize-space(string(.))])'):
                    found = True
                    break
            except Exception as e:   # a dynamic error on this record (bad JSON in a string, say): try the next
                error = error or str(e).splitlines()[0][:160]
        if not found:
            hint = (" JSON held in a string field is read as json-to-xml(*[@key='field'])/*/*[@key='name']."
                    if 'json-to-xml' in x else '')
            warnings.append(f"xpath {_shown(x, 200)} (used for {used[0]}{' and more' if len(used) > 1 else ''}) selects nothing in "
                            f"any of the {len(parsed)} sample records{f' ({error})' if error else ''}.{hint}")
    return warnings
