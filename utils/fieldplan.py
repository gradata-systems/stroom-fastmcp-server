"""A backend-neutral field plan for an index, rendered for Lucene or Elasticsearch.

Each field has a name (as the index will store it), a logical type, and the XPath in the event it
comes from. The same plan becomes a Lucene index doc's field list or an Elasticsearch index template,
plus a draft indexing XSLT in the output form each backend's indexing filter reads.
"""
from typing import Any, Literal

from pydantic import BaseModel, Field

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


class FieldPlan(BaseModel):
    backend: Backend
    index_name: str = Field(description="Lucene index doc name, or Elasticsearch index / data stream name.")
    time_field: str = Field(description="Name of the field holding the event time.")
    fields: list[PlannedField]

    def required(self) -> list[str]:
        """Problems that would stop indexing or verification from working."""
        names = {f.name for f in self.fields}
        problems = [f"Missing required field '{n}'" for n in ('StreamId', 'EventId') if n not in names]
        if self.time_field not in names:
            problems.append(f"The time field '{self.time_field}' is not in the plan")
        if self.backend == 'elasticsearch' and '@timestamp' not in names:
            problems.append("Elasticsearch data streams need '@timestamp'")
        return problems

    def lucene_fields(self) -> list[dict[str, Any]]:
        return [{'fldName': f.name, 'fldType': LUCENE[f.type][0], 'analyzerType': LUCENE[f.type][1],
                 'indexed': True, 'stored': True, 'caseSensitive': False} for f in self.fields]

    def elastic_template(self, template_name: str, priority: int = 200) -> dict[str, Any]:
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

    def xslt(self, version: str = '3.0') -> str:
        """A draft indexing XSLT reading Events (xpath-default-namespace event-logging:3)."""
        if self.backend == 'lucene':
            body = '\n'.join(f'      <data name="{f.name}" value="{{{f.source}}}" />' for f in self.fields)
            return f"""<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xpath-default-namespace="event-logging:3" xmlns="records:2" xmlns:stroom="stroom"
    xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" version="{version}">
  <xsl:template match="/Events">
    <records xsi:schemaLocation="records:2 file://records-v2.0.xsd" version="2.0">
      <xsl:apply-templates select="Event" />
    </records>
  </xsl:template>
  <xsl:template match="Event">
    <record>
{body}
    </record>
  </xsl:template>
</xsl:stylesheet>
"""
        lines = []
        for f in self.fields:
            element = _ES_JSON_ELEMENT.get(f.type, 'string')
            lines.append(f'      <xsl:if test="{f.source}"><{element} key="{f.name}">'
                         f'<xsl:value-of select="{f.source}" /></{element}></xsl:if>')
        body = '\n'.join(lines)
        return f"""<?xml version="1.1" encoding="UTF-8"?>
<xsl:stylesheet xpath-default-namespace="event-logging:3" xmlns="http://www.w3.org/2005/xpath-functions"
    xmlns:stroom="stroom" xmlns:xsl="http://www.w3.org/1999/XSL/Transform" version="{version}">
  <xsl:template match="/Events">
    <array>
      <xsl:apply-templates select="Event" />
    </array>
  </xsl:template>
  <xsl:template match="Event">
    <map>
{body}
    </map>
  </xsl:template>
</xsl:stylesheet>
"""
