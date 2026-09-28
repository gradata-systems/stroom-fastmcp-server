"""XML schemas held in Stroom, loaded for local validation with lxml."""
import re
from typing import Any

from fastmcp.exceptions import ToolError
from lxml import etree

from utils.stroom import StroomGateway

_SCHEMA_LOCATION = re.compile(r'schemaLocation="\s*(\S+)\s+(\S+)\s*"')


class SchemaCache:
    """Compiled XSDs keyed by system id (e.g. 'file://event-logging-v3.5.2.xsd').

    Stroom resolves an XML document's xsi:schemaLocation against its XMLSchema docs by system id,
    so doing the same here validates against exactly what the instance has installed.
    """

    def __init__(self, stroom: StroomGateway):
        self._stroom = stroom
        self._index: dict[str, str] | None = None  # system id -> doc uuid
        self._compiled: dict[str, etree.XMLSchema] = {}
        self._sources: dict[str, str] = {}

    async def _load_index(self) -> dict[str, str]:
        if self._index is None:
            found = await self._stroom.find_documents('*', ['XMLSchema'], 500)
            self._index = {}
            for value in found.get('values') or []:
                ref = value['docRef']
                if ref.get('type') != 'XMLSchema':
                    continue
                doc = await self._stroom.get(f"/xmlSchema/v1/{ref['uuid']}")
                if doc.get('systemId'):
                    self._index[doc['systemId']] = ref['uuid']
        return self._index

    async def system_ids(self) -> list[str]:
        return sorted(await self._load_index())

    async def source(self, system_id: str) -> str:
        """The XSD text itself."""
        if system_id not in self._sources:
            index = await self._load_index()
            if system_id not in index:
                known = ', '.join(s for s in sorted(index) if 'event-logging' in s) or 'none'
                raise ToolError(f"Stroom has no XML schema '{system_id}'. Event-logging schemas available: {known}")
            self._sources[system_id] = (await self._stroom.get(f'/xmlSchema/v1/{index[system_id]}'))['data']
        return self._sources[system_id]

    async def get(self, system_id: str) -> etree.XMLSchema:
        if system_id not in self._compiled:
            self._compiled[system_id] = etree.XMLSchema(etree.fromstring((await self.source(system_id)).encode()))
        return self._compiled[system_id]


def declared_system_id(xml: str) -> str | None:
    """The system id named in a document's xsi:schemaLocation, if any."""
    match = _SCHEMA_LOCATION.search(xml[:4000])
    return match.group(2) if match else None


def event_logging_system_id(version: str) -> str:
    return f'file://event-logging-v{version}.xsd'


def errors_for(schema: etree.XMLSchema, doc: Any) -> list[dict[str, Any]]:
    schema.validate(doc)
    return [{'line': e.line, 'path': e.path, 'message': e.message} for e in schema.error_log]
