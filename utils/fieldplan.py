"""A backend-neutral field plan for an index, rendered for Lucene or Elasticsearch.

Each field has a name (as the index will store it), a logical type, and the XPath in the event it
comes from. The same plan becomes a Lucene index doc's field list or an Elasticsearch index template,
plus a draft indexing XSLT in the output form each backend's indexing filter reads.

A discovery plan instead indexes raw structured data (JSON) as it is, into Elasticsearch: every record is
copied with its own field names and Elasticsearch maps them dynamically, so nothing is surveyed up front.
Only StreamId, EventId and @timestamp are mapped explicitly.
"""
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from utils.xsltgen import SharedTemplate

LogicalType = Literal['id', 'keyword', 'text', 'date', 'long', 'double', 'boolean', 'ip']
Backend = Literal['lucene', 'elasticsearch']

# Lucene has no working KEYWORD field type: a keyword is TEXT with the KEYWORD analyzer (spike finding).
LUCENE = {
    'id': ('ID', 'KEYWORD'), 'keyword': ('TEXT', 'KEYWORD'), 'text': ('TEXT', 'ALPHA_NUMERIC'),
    'date': ('DATE', 'KEYWORD'), 'long': ('LONG', 'KEYWORD'), 'double': ('DOUBLE', 'KEYWORD'),
    'boolean': ('TEXT', 'KEYWORD'), 'ip': ('TEXT', 'KEYWORD'),
}
ELASTIC = {'id': 'long', 'keyword': 'keyword', 'text': 'text', 'date': 'date', 'long': 'long',
           'double': 'double', 'boolean': 'boolean', 'ip': 'ip'}
_ES_JSON_ELEMENT = {'id': 'number', 'long': 'number', 'double': 'number', 'boolean': 'boolean'}


class PlannedField(BaseModel):
    name: str = Field(description="Field name in the index, e.g. 'UserId' or 'user.name'.")
    type: LogicalType
    source: str = Field(description="XPath from the Event element, e.g. 'EventSource/User/Id' or '@StreamId'.")
    description: str = ''


class Discovery(BaseModel):
    """A discovery index: raw JSON records indexed as they are, mapped dynamically by Elasticsearch."""
    timestamp_field: str = Field(description=(
        "The source field holding the event time, as the user names it; dotted for a nested one, e.g. 'ts' or "
        "'event.created'. Indexed as @timestamp."))
    timestamp_format: str | None = Field(default=None, description=(
        "Its Java date pattern when it is not ISO 8601 or epoch milliseconds, e.g. 'dd/MM/yyyy HH:mm:ss'."))
    meta: dict[str, str] = Field(default_factory=dict, description=(
        "Stream meta to add to each document, index field -> meta attribute (describe_stream lists them), e.g. "
        "{'stroom.feed': 'Feed'}."))
    drop: list[str] = Field(default_factory=list, description="Top-level source fields to leave out.")
    unpack_json: bool = Field(default=True, description=(
        "Parse a string holding a JSON object into a sibling '<field>_json' object; the string is kept."))
    ignore_above: int = Field(default=1024, ge=1, description="Strings are keywords; longer ones are not indexed.")
    total_fields_limit: int = Field(default=2000, ge=100, description="Elasticsearch's limit on mapped fields.")


# Elasticsearch's metadata fields: a document holding one at its top level is rejected.
ES_METADATA = ('_id', '_index', '_source', '_routing', '_ignored', '_ignored_source', '_seq_no', '_primary_term',
               '_version', '_field_names', '_doc_count', '_tier', '_data_stream_timestamp', '_meta', '_nested_path',
               '_tsid', '_size', '_feature')
DISCOVERY_FIELDS = (('StreamId', 'id', 'stroom:stream-id()'), ('EventId', 'id', 'stroom:record-no()'),
                    ('@timestamp', 'date', ''))


def _call(use: SharedTemplate, indent: str) -> str:
    """A call to a shared XSLT's named template, with the parameters the environment passes it."""
    if not use.with_params:
        return f'{indent}<xsl:call-template name="{use.template}" />'
    params = ''.join(f'<xsl:with-param name="{n}" select="{s}" />' for n, s in use.with_params.items())
    return f'{indent}<xsl:call-template name="{use.template}">{params}</xsl:call-template>'


def _json_path(dotted: str) -> str:
    """An XPath to a JSONParser field: 'event.created' -> *[@key='event']/*[@key='created']."""
    return '/'.join(f"*[@key={json.dumps(part).replace(chr(34), chr(39))}]" for part in dotted.split('.'))


class FieldPlan(BaseModel):
    backend: Backend
    index_name: str = Field(description="Lucene index doc name, or Elasticsearch index / data stream name.")
    time_field: str = Field(description="Name of the field holding the event time.")
    fields: list[PlannedField]
    drop_when: list[str] = Field(default_factory=list, description=(
        "XPath tests on an Event (event-logging:3 is the default namespace) for events the index must not hold, e.g. "
        "\"EventDetail/TypeId = 'Heartbeat'\" or \"EventSource/User/Id = 'monitor'\"; any that holds drops the event."))
    discovery: Discovery | None = Field(default=None, description="Set for a discovery index (draft_index_mapping).")
    shared: list[SharedTemplate] = Field(default_factory=list, description=(
        "Named templates from shared XSLTs (xsl:import) the environment's indexing XSLTs call, each writing a field "
        "(at: its name, e.g. 'guid'); the XSLT calls them instead of writing those fields itself."))
    nest: bool = Field(default=True, description=(
        "Elasticsearch: write dotted names as nested objects (user.id, user.name -> \"user\": {\"id\", \"name\"}); "
        "false writes them as flat dotted keys, for an index template with subobjects: false."))

    @classmethod
    def for_discovery(cls, index_name: str, discovery: Discovery) -> 'FieldPlan':
        fields = [PlannedField(name=n, type=t, source=s or discovery.timestamp_field) for n, t, s in DISCOVERY_FIELDS]
        return cls(backend='elasticsearch', index_name=index_name, time_field='@timestamp', fields=fields,
                   discovery=discovery)

    def events(self) -> str:
        """The apply-templates select: every Event, less the ones drop_when names."""
        if not self.drop_when:
            return 'Event'
        tests = ' or '.join(f'({t})' for t in self.drop_when)
        return f'Event[not({tests})]'.replace('&', '&amp;').replace('<', '&lt;').replace('"', '&quot;')

    def written_by(self, name: str) -> SharedTemplate | None:
        """The shared template that writes this field (or the object holding it), if any."""
        return next((u for u in self.shared if name == u.at or name.startswith(u.at + '.')), None)

    def _imports(self) -> str:
        return ''.join(f'  <xsl:import href="{h}" />\n' for h in dict.fromkeys(u.href for u in self.shared))

    def required(self) -> list[str]:
        """Problems that would stop indexing or verification from working."""
        names = {f.name for f in self.fields}
        names |= {u.at for u in self.shared if not any(n == u.at or n.startswith(u.at + '.') for n in names)}
        problems = [f"Missing required field '{n}'" for n in ('StreamId', 'EventId') if n not in names]
        if self.time_field not in names:
            problems.append(f"The time field '{self.time_field}' is not in the plan")
        if self.backend == 'elasticsearch' and '@timestamp' not in names:
            problems.append("Elasticsearch data streams need '@timestamp'")
        if self.backend == 'elasticsearch':
            hidden = sorted(n for n in names if any(part.startswith('_') for part in n.split('.')))
            if hidden:
                problems.append(f"{hidden}: Stroom's Elasticsearch indexing filter drops fields whose names start with _ "
                                f"(silently); rename them")
            for name in sorted(names):
                inner = sorted(n for n in names if n.startswith(name + '.'))
                if inner:
                    problems.append(f"'{name}' is a value and also the object holding {inner}: an Elasticsearch field "
                                    f"cannot be both; rename one")
        return problems

    def lucene_fields(self) -> list[dict[str, Any]]:
        return [{'fldName': f.name, 'fldType': LUCENE[f.type][0], 'analyzerType': LUCENE[f.type][1],
                 'indexed': True, 'stored': True, 'caseSensitive': False} for f in self.fields]

    def elastic_template(self, template_name: str, priority: int = 200) -> dict[str, Any]:
        if self.discovery:
            return {'name': template_name, 'body': self._discovery_template(priority)}
        properties: dict[str, Any] = {}
        for f in self.fields:
            node = properties
            parts = f.name.split('.')
            for part in parts[:-1]:
                node = node.setdefault(part, {'properties': {}})['properties']
            node[parts[-1]] = {'type': ELASTIC[f.type]}
        return {'name': template_name, 'body': {
            'index_patterns': [f'{self.index_name}*'], 'priority': priority,
            'template': {'mappings': {'dynamic': False, 'properties': properties}}}}

    def _discovery_template(self, priority: int) -> dict[str, Any]:
        """Permissive: dynamic mapping, strings as keywords, guardrails; explicit types only for the fields Stroom
        needs (StreamId and EventId to find each record again, @timestamp for time)."""
        d = self.discovery
        return {'index_patterns': [f'{self.index_name}*'], 'priority': priority, 'template': {
            'settings': {'index': {'mapping': {'total_fields': {'limit': d.total_fields_limit},
                                               'ignore_malformed': True}}},
            'mappings': {'dynamic': True, 'date_detection': False, 'dynamic_templates': [
                {'strings_as_keywords': {'match_mapping_type': 'string',
                                         'mapping': {'type': 'keyword', 'ignore_above': d.ignore_above}}}],
                'properties': {f.name: {'type': ELASTIC[f.type]} for f in self.fields}}}}

    def _discovery_xslt(self, version: str) -> str:
        """Copies each JSONParser record into the xpath-functions JSON the Elasticsearch filter reads, with its own
        field names, adding StreamId, EventId (the record number) and @timestamp, and any stream meta."""
        d = self.discovery
        when = _json_path(d.timestamp_field)
        time = (f"stroom:format-date(string({when}), '{d.timestamp_format}')" if d.timestamp_format
                else f"string({when})")
        meta = '\n'.join(f'      <string key="{name}"><xsl:value-of select="stroom:meta(\'{attr}\')" /></string>'
                         for name, attr in d.meta.items())
        dropped = ', '.join(json.dumps(k).replace('"', "'") for k in d.drop)
        select = f"*[not(@key = ({dropped}))]" if d.drop else '*'
        # Top-level keys to rename (see the template below).
        ours = [f.name for f in self.fields] + list(d.meta)
        clash = ', '.join(json.dumps(k).replace('"', "'") for k in list(ES_METADATA) + ours)
        unpack = '' if not d.unpack_json else """
  <!-- A string holding a JSON object is also parsed, into a sibling <key>_json object: the string is kept, so a
       field that is only sometimes JSON keeps one type. Text that is not valid JSON is left as it is. -->
  <xsl:template match="j:string[@key][starts-with(normalize-space(.), '{')][ends-with(normalize-space(.), '}')]" mode="copy">
    <string key="{@key}"><xsl:value-of select="." /></string>
    <xsl:try>
      <xsl:variable name="parsed" select="json-to-xml(string(.))" />
      <map key="{@key}_json"><xsl:copy-of select="$parsed/fn:map/*" /></map>
      <xsl:catch />
    </xsl:try>
  </xsl:template>
"""
        return f"""<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xmlns="http://www.w3.org/2005/xpath-functions" xmlns:fn="http://www.w3.org/2005/xpath-functions"
    xmlns:j="http://www.w3.org/2013/XSL/json" xmlns:stroom="stroom" xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
    xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:d="urn:stroom-mcp:discovery"
    xpath-default-namespace="http://www.w3.org/2013/XSL/json" exclude-result-prefixes="j fn stroom xs d" version="{version}">
  <!-- Discovery index {self.index_name}: each raw record as it is, mapped dynamically by Elasticsearch. -->
  <xsl:template match="/">
    <array>
      <xsl:apply-templates select="array/map | map" mode="record" />
    </array>
  </xsl:template>
  <xsl:template match="map" mode="record">
    <map>
      <number key="StreamId"><xsl:value-of select="stroom:stream-id()" /></number>
      <number key="EventId"><xsl:value-of select="stroom:record-no()" /></number>
      <xsl:if test="{when}"><string key="@timestamp"><xsl:value-of select="{time}" /></string></xsl:if>
{meta}
      <xsl:apply-templates select="{select}" mode="copy" />
    </map>
  </xsl:template>
  <!-- Top-level keys Elasticsearch refuses (its metadata fields), that Stroom's indexing filter drops (any starting
       with _), or that would repeat a field written here: kept, as <key>_original without the leading _. -->
  <xsl:template match="map[parent::array/parent::document-node() or parent::document-node()]/*[@key = ({clash})
                       or starts-with(@key, '_')]" mode="copy" priority="3">
    <xsl:variable name="bare" select="replace(@key, '^_+', '')" />
    <xsl:element name="{{local-name()}}">
      <xsl:attribute name="key" select="concat(if ($bare = '') then 'underscore' else $bare, '_original')" />
      <xsl:apply-templates mode="copy" />
    </xsl:element>
  </xsl:template>
  <xsl:template match="*" mode="copy">
    <xsl:element name="{{local-name()}}">
      <xsl:if test="@key"><xsl:attribute name="key" select="d:key(@key)" /></xsl:if>
      <xsl:apply-templates mode="copy" />
    </xsl:element>
  </xsl:template>
  <!-- Keys Elasticsearch cannot take as field names (empty, or with an empty dotted part: .a, a..b) are repaired;
       keys starting with _, which Stroom's indexing filter drops at any depth, become <key>_original without it. -->
  <xsl:function name="d:key" as="xs:string">
    <xsl:param name="key" as="xs:string" />
    <xsl:variable name="joined" select="replace(replace($key, '\\.{{2,}}', '.'), '^\\.+', '')" />
    <xsl:variable name="bare" select="replace($joined, '^_+', '')" />
    <xsl:sequence select="if (normalize-space($joined) = '') then 'empty_key'
                          else if (starts-with($joined, '_')) then concat(if ($bare = '') then 'underscore' else $bare, '_original')
                          else $joined" />
  </xsl:function>{unpack}
</xsl:stylesheet>
"""

    def _elastic_lines(self) -> list[str]:
        """The document's fields: dotted names nested as objects (user.id -> <map key="user"><string key="id">),
        each object written only when one of its fields is present, unless nest is off (flat dotted keys). A name
        that is also another field's object stays flat (required() reports it)."""
        def leaf(f: Any, key: str, indent: str) -> str:
            if isinstance(f, SharedTemplate):
                return _call(f, indent)
            element = _ES_JSON_ELEMENT.get(f.type, 'string')
            return (f'{indent}<xsl:if test="{f.source}"><{element} key="{key}">'
                    f'<xsl:value-of select="{f.source}" /></{element}></xsl:if>')
        # A field a shared template writes is not written here: the template is called in its place.
        items: list[Any] = [f for f in self.fields if not self.written_by(f.name)] + list(self.shared)

        def name_of(item: Any) -> str:
            return item.at if isinstance(item, SharedTemplate) else item.name
        names = {name_of(i) for i in items}
        flat = [i for i in items if not self.nest or '.' not in name_of(i)
                or any('.'.join(name_of(i).split('.')[:n]) in names for n in range(1, name_of(i).count('.') + 1))]
        tree: dict[str, Any] = {}
        for item in items:
            if any(item is f for f in flat):
                tree[name_of(item)] = item
                continue
            node = tree
            parts = name_of(item).split('.')
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = item

        def sources(node: dict[str, Any]) -> list[str | None]:
            return [s for v in node.values()
                    for s in ([None] if isinstance(v, SharedTemplate) else [v.source] if isinstance(v, PlannedField)
                              else sources(v))]

        def render(node: dict[str, Any], indent: str) -> list[str]:
            out = []
            for key, value in node.items():
                if not isinstance(value, dict):
                    out.append(leaf(value, key, indent))
                    continue
                tests = sources(value)
                if None in tests:       # a shared template inside: written whatever the event holds
                    out += [f'{indent}<map key="{key}">', *render(value, indent + '  '), f'{indent}</map>']
                else:
                    out += [f'{indent}<xsl:if test="{' or '.join(dict.fromkeys(tests))}">', f'{indent}  <map key="{key}">',
                            *render(value, indent + '    '), f'{indent}  </map>', f'{indent}</xsl:if>']
            return out
        return render(tree, '      ')

    def xslt(self, version: str = '3.0') -> str:
        """A draft indexing XSLT reading Events (xpath-default-namespace event-logging:3), or for a discovery
        index, reading the JSONParser's records."""
        if self.discovery:
            return self._discovery_xslt(version)
        if self.backend == 'lucene':
            body = '\n'.join([f'      <data name="{f.name}" value="{{{f.source}}}" />' for f in self.fields
                              if not self.written_by(f.name)]
                             + [_call(u, '      ') for u in self.shared])
            return f"""<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xpath-default-namespace="event-logging:3" xmlns="records:2" xmlns:stroom="stroom"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" version="{version}">
{self._imports()}  <xsl:template match="/Events">
    <records xsi:schemaLocation="records:2 file://records-v2.0.xsd" version="2.0">
      <xsl:apply-templates select="{self.events()}" />
    </records>
  </xsl:template>
  <xsl:template match="Event">
    <record>
{body}
    </record>
  </xsl:template>
</xsl:stylesheet>
"""
        body = '\n'.join(self._elastic_lines())
        return f"""<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xpath-default-namespace="event-logging:3" xmlns="http://www.w3.org/2005/xpath-functions"
    xmlns:stroom="stroom" xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="{version}">
{self._imports()}  <xsl:template match="/Events">
    <array>
      <xsl:apply-templates select="{self.events()}" />
    </array>
  </xsl:template>
  <xsl:template match="Event">
    <map>
{body}
    </map>
  </xsl:template>
</xsl:stylesheet>
"""
