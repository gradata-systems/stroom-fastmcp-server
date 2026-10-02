"""Generate a reference-data XSLT: the translation of a reference feed (a user list, an asset register) into the
reference-data:2 maps that stroom:lookup() reads from an events pipeline.

A Reference Data pipeline parses the feed as any other (Data Splitter or JSON) and its XSLT writes one
<reference> per record and map: the map's name, the key, and the value (text, or elements). The events
pipeline then names the reference feed and the standard Reference Loader as a pipeline reference, and its
translation looks keys up with stroom:lookup('MAP', key).
"""
from typing import Literal

from lxml import etree
from pydantic import BaseModel, Field, model_validator

from utils.xsltgen import INPUT_NAMESPACE, JSON_RECORDS, XSI, XSL, Condition, DropRule, literal

REF = 'reference-data:2'


class ReferenceValue(BaseModel):
    """One part of a map's value: text, or an element named `element` under <value>."""
    element: str = Field('', description="Element name under the value, e.g. 'department'; '' for a plain text "
                                         "value (a map has at most one of those).")
    field: str | None = Field(None, description="Input field (Data Splitter name or JSON key).")
    value: str | None = Field(None, description="Or a constant.")
    xpath: str | None = Field(None, description="Or an XPath relative to the record.")

    @model_validator(mode='after')
    def one_source(self):
        if sum(x is not None for x in (self.field, self.value, self.xpath)) != 1:
            raise ValueError(f"value '{self.element or 'text'}': give exactly one of field, value or xpath")
        return self


class ReferenceMap(BaseModel):
    name: str = Field(description="The map name stroom:lookup() uses, e.g. 'USER_TO_DEPARTMENT'.")
    key: str | None = Field(None, description="Input field holding the key.")
    key_xpath: str | None = Field(None, description="Or an XPath for the key, e.g. lower-case(data[@name='user']/@value).")
    values: list[ReferenceValue] = Field(min_length=1, description="What the key maps to.")

    @model_validator(mode='after')
    def one_key(self):
        if (self.key is None) == (self.key_xpath is None):
            raise ValueError(f"map '{self.name}': give exactly one of key or key_xpath")
        if sum(1 for v in self.values if not v.element) > 1:
            raise ValueError(f"map '{self.name}': at most one text value; name the others as elements")
        return self


class ReferenceMapping(BaseModel):
    input: Literal['data_splitter', 'json'] = Field(description="How the reference feed is parsed.")
    json_layout: Literal['array', 'lines'] = 'array'
    root: str | None = Field(None, description="Root to match; defaults as for build_translation_xslt.")
    record: str | None = Field(None, description="Record elements; defaults as for build_translation_xslt.")
    maps: list[ReferenceMap] = Field(min_length=1)
    drop_when: list[DropRule] = Field(default_factory=list, description="Records to leave out of the reference data "
                                                                        "(disabled accounts, say); each entry's conditions "
                                                                        "must all hold.")


def _selector(mapping: ReferenceMapping, field: str) -> str:
    if mapping.input == 'data_splitter':
        return '/'.join(f"data[@name={literal(p)}]" for p in field.split('/')) + '/@value'
    return '/'.join(f"*[@key={literal(p)}]" for p in field.split('.'))


def _test(mapping: ReferenceMapping, c: Condition, problems: list[str]) -> str:
    src = c.xpath or _selector(mapping, c.field)
    if c.equals is not None:
        return f"{src} = {literal(c.equals)}"
    if c.one_of is not None:
        return f"{src} = ({', '.join(literal(v) for v in c.one_of)})"
    if c.matches is not None:
        return f"exists({src}[matches(., {literal(c.matches)})])"
    if c.present is not None:
        test = f"exists({src}[normalize-space(.)])"
        return test if c.present else f"not({test})"
    problems.append(f"drop_when: a reference mapping's conditions take equals, one_of, matches or present, not in_dictionary")
    return 'false()'


def generate_reference(mapping: ReferenceMapping, schema_version: str = '2.0.1') -> dict:
    """{'ok', 'problems', 'xslt', 'maps'}: the XSLT writing reference-data:2 for the mapping's maps."""
    problems: list[str] = []
    seen = set()
    for m in mapping.maps:
        if m.name in seen:
            problems.append(f"map '{m.name}' is defined twice")
        seen.add(m.name)
    if problems:
        return {'ok': False, 'problems': problems, 'xslt': None, 'maps': sorted(seen)}
    nsmap = {None: REF, 'xsl': XSL, 'xsi': XSI, 'stroom': 'stroom'}
    sheet = etree.Element(f'{{{XSL}}}stylesheet', nsmap=nsmap, version='3.0')
    sheet.set('xpath-default-namespace', INPUT_NAMESPACE[mapping.input])
    sheet.set('exclude-result-prefixes', 'stroom')
    root_match = mapping.root or ('records' if mapping.input == 'data_splitter' else '/')
    records = mapping.record or ('record' if mapping.input == 'data_splitter' else JSON_RECORDS[mapping.json_layout])
    root_template = etree.SubElement(sheet, f'{{{XSL}}}template', match=root_match)
    data = etree.SubElement(root_template, f'{{{REF}}}referenceData', version=schema_version)
    data.set(f'{{{XSI}}}schemaLocation', f'{REF} file://reference-data-v{schema_version}.xsd')
    etree.SubElement(data, f'{{{XSL}}}apply-templates', select=records)
    record_template = etree.SubElement(sheet, f'{{{XSL}}}template', match='*')
    if mapping.drop_when:
        drops = ' or '.join(f"({' and '.join(_test(mapping, c, problems) for c in d.when)})" for d in mapping.drop_when)
        record_template.append(etree.Comment(' left out: ' + '; '.join(d.reason for d in mapping.drop_when) + ' '))
        record_template = etree.SubElement(record_template, f'{{{XSL}}}if', test=f"not({drops})")
    if problems:
        return {'ok': False, 'problems': problems, 'xslt': None, 'maps': sorted(seen)}
    for m in mapping.maps:
        key = m.key_xpath or _selector(mapping, m.key)
        guard = etree.SubElement(record_template, f'{{{XSL}}}if', test=f"normalize-space({key})")
        reference = etree.SubElement(guard, f'{{{REF}}}reference')
        etree.SubElement(reference, f'{{{REF}}}map').text = m.name
        etree.SubElement(etree.SubElement(reference, f'{{{REF}}}key'), f'{{{XSL}}}value-of', select=key)
        value = etree.SubElement(reference, f'{{{REF}}}value')
        # The schema allows one element inside <value>: several named parts go inside a <details> element. Elements
        # stay in no namespace; a lookup's `path` finds them at any depth (stroom:lookup(...)//*:department).
        named = [p for p in m.values if p.element]
        parent = etree.SubElement(value, f'{{{XSL}}}element', name='details', namespace='') if len(named) > 1 else value
        for part in m.values:
            holder = parent if not part.element else etree.SubElement(parent, f'{{{XSL}}}element', name=part.element, namespace='')
            if part.value is not None:
                holder.text = part.value
            else:
                select = part.xpath or _selector(mapping, part.field)
                etree.SubElement(holder, f'{{{XSL}}}value-of', select=select)
    xslt = etree.tostring(sheet, pretty_print=True, xml_declaration=True, encoding='UTF-8').decode('utf-8')
    return {'ok': True, 'problems': [], 'xslt': xslt, 'maps': [m.name for m in mapping.maps]}
