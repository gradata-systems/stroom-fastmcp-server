"""XML schemas held in Stroom, loaded for local validation with lxml."""
import re
from typing import Any

from fastmcp.exceptions import ToolError
from lxml import etree

from utils.stroom import StroomGateway

_SCHEMA_LOCATION = re.compile(r'schemaLocation="\s*(\S+)\s+(\S+)\s*"')
_EVENT_LOGGING_VERSION = re.compile(r'event-logging-v(\d+(?:\.\d+)*)\.xsd')
XSI = 'http://www.w3.org/2001/XMLSchema-instance'
# Seen on Stroom 7.13: output naming file:///event-logging-v4.1.0.xsd (three slashes) or event-logging-v4.1.0.xsd (no
# file:) was not refused as an unknown schema; the schema filter validated it against event-logging 3.2.3's instead,
# whose errors ("Value '4.1.0' is not facet-valid ... [3.0.0, ..., 3.2.3]") hide the cause.
FALLBACK = ("Stroom matches the system id exactly, and for one it doesn't hold its schema filter validates against "
            "another schema of the namespace (seen: event-logging 3.2.3's, refusing Version 4.1.0), so the errors "
            "mislead.")


class SchemaCache:
    """Compiled XSDs keyed by system id (e.g. 'file://event-logging-v3.5.2.xsd').

    Stroom resolves an XML document's xsi:schemaLocation against its XMLSchema docs by system id,
    so doing the same here validates against exactly what the instance has installed.
    """

    def __init__(self, stroom: StroomGateway):
        self._stroom = stroom
        self._index: dict[str, str] | None = None  # system id -> doc uuid
        self._namespaces: dict[str, str] = {}      # system id -> its namespace
        self._compiled: dict[str, etree.XMLSchema] = {}
        self._sources: dict[str, str] = {}

    async def _load_index(self) -> dict[str, str]:
        if self._index is None:
            found = await self._stroom.find_documents('*', ['XMLSchema'], 500)
            # Built apart and kept only once whole: the cache serves every user, and a half-built index kept after one
            # failed read said "Stroom has no XML schema" to all of them until a restart.
            index: dict[str, str] = {}
            namespaces: dict[str, str] = {}
            for value in found.get('values') or []:
                ref = value.get('docRef') or {}
                if ref.get('type') != 'XMLSchema' or not ref.get('uuid'):
                    continue
                try:
                    doc = await self._stroom.get(f"/xmlSchema/v1/{ref['uuid']}")
                except ToolError:   # deleted since the search listed it, or not this user's to read: not one of them
                    continue
                if (doc or {}).get('systemId'):
                    index[doc['systemId']] = ref['uuid']
                    if doc.get('namespaceURI'):
                        namespaces[doc['systemId']] = doc['namespaceURI']
            self._index, self._namespaces = index, namespaces
        return self._index

    async def system_ids(self) -> list[str]:
        return sorted(await self._load_index())

    async def event_versions(self) -> list[str]:
        """The event-logging versions this Stroom holds, newest first."""
        return newest_first(v for v in map(event_logging_version, await self._load_index()) if v)

    async def location_problems(self, root: etree._Element) -> list[str]:
        """The xsi:schemaLocation pairs in a document (an XSLT's literal output elements too) naming a system id this
        Stroom holds no schema for, in a namespace it holds schemas for: Stroom would validate against another of
        them. A namespace it holds none for (or schemas this user can't read) is not judged."""
        index = await self._load_index()
        held: dict[str, list[str]] = {}
        for system_id, namespace in self._namespaces.items():
            held.setdefault(namespace, []).append(system_id)
        problems = []
        for element in root.iter():
            value = element.get(f'{{{XSI}}}schemaLocation') if isinstance(element.tag, str) else None
            if not value or '{' in value:      # an attribute value template: known only when it runs
                continue
            parts = value.split()
            for namespace, system_id in zip(parts[0::2], parts[1::2]):
                if system_id not in index and namespace in held:
                    problems.append(unheld_location(system_id, held[namespace]))
        return problems

    async def source(self, system_id: str) -> str:
        """The XSD text itself."""
        if system_id not in self._sources:
            index = await self._load_index()
            if system_id not in index:
                fixed = _two_slashes(system_id)
                if fixed in index:
                    raise ToolError(unheld_location(system_id, [fixed]))
                known = ', '.join(newest_first(v for v in map(event_logging_version, index) if v)) or 'none'
                raise ToolError(f"Stroom has no XML schema '{system_id}'. Event-logging versions it holds, newest "
                                f"first: {known} (all in namespace event-logging:3, 4.x as much as 3.x)")
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


def event_logging_version(text: str | None) -> str | None:
    """The event-logging version a system id names, or the first an XSLT or Events document names, e.g. '3.5.2'."""
    match = _EVENT_LOGGING_VERSION.search(text or '')
    return match.group(1) if match else None


def newest_first(versions: Any) -> list[str]:
    return sorted(set(versions), key=lambda v: tuple(int(p) for p in v.split('.')), reverse=True)


def _two_slashes(system_id: str) -> str:
    """A system id as Stroom names its schemas, file:// and the file name: from file:///x.xsd or a bare x.xsd."""
    name = re.sub(r'^file:/*', '', system_id)
    return f'file://{name}'


def unheld_location(system_id: str, held: list[str]) -> str:
    """Why a schema location naming a system id Stroom doesn't hold fails, and what to write instead."""
    fixed = _two_slashes(system_id)
    if fixed in held:
        return f"xsi:schemaLocation names '{system_id}': write '{fixed}' (file: and two slashes). {FALLBACK}"
    ordered = sorted(held, key=lambda s: [int(n) for n in re.findall(r'\d+', s)], reverse=True)
    return (f"xsi:schemaLocation names '{system_id}', a schema this Stroom doesn't hold; for that namespace it "
            f"holds {', '.join(ordered)}. {FALLBACK}")


def errors_for(schema: etree.XMLSchema, doc: Any) -> list[dict[str, Any]]:
    schema.validate(doc)
    return [{'line': e.line, 'path': e.path, 'message': e.message} for e in schema.error_log]
