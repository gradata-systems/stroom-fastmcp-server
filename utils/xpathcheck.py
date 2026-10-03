"""Evaluate a mapping's xpath inputs against the sample, before anything is created or stepped.

An xpath that selects nothing in any sample record (json-to-xml(event)/timestamp where the JSON parser gives
string[@key='event'], say) generates valid XSLT that steps clean and writes empty elements or matches no rule.
Each record is rebuilt the way the pipeline's parser presents it to the XSLT (the JSONParser's map/string
elements, the Data Splitter's records:2 data elements, or the XML record itself) and every xpath is evaluated on
it with Saxon, as the translation would. Expressions that need Stroom (stroom: functions) or variables are not
evaluated, nor are for_each mappings, whose xpaths read items.
"""
import json
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from lxml import etree

from utils.dsgen import SplitterSpec, dry_run
from utils.localcheck import sample_records
from utils.xsltgen import INPUT_NAMESPACE, TranslationMapping

_processor = None


def _saxon():
    global _processor
    if _processor is None:
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
    ns = INPUT_NAMESPACE.get(mapping.input, mapping.xml_namespace)
    if mapping.input == 'json':
        return [json_xml(v).replace('<map', f'<map xmlns="{ns}"', 1) for t in texts for v in _json_values(t)]
    if mapping.input == 'data_splitter':
        if splitter is None:
            return []
        return [f'<record xmlns="{ns}">' + ''.join(f'<data name={quoteattr(n)} value={quoteattr(str(v))}/>'
                                                   for n, v in r.items()) + '</record>'
                for t in texts for r in dry_run(splitter, t.strip('﻿\r\n '))['records']]
    records, _ = sample_records(mapping, sample, splitter)
    return [etree.tostring(r, encoding='unicode') for r in records if isinstance(r, etree._Element)]


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
    return {x: u for x, u in used.items() if 'stroom:' not in x and '$' not in x}


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
    ns = INPUT_NAMESPACE.get(mapping.input, mapping.xml_namespace)
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
            warnings.append(f"xpath {x!r} (used for {used[0]}{' and more' if len(used) > 1 else ''}) selects nothing in "
                            f"any of the {len(parsed)} sample records{f' ({error})' if error else ''}.{hint}")
    return warnings
