"""Group Stroom error markers and classify each group as blocking, review or benign."""
import fnmatch
import re
from collections import Counter
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

Severity = Literal['FATAL', 'ERROR', 'WARNING', 'INFO']
Classification = Literal['blocking', 'review', 'benign']
_ORDER = {'blocking': 0, 'review': 1, 'benign': 2}
# Stroom reports WARN in some places and WARNING in others.
_SEVERITY = {'WARN': 'WARNING'}


class Rule(BaseModel):
    severity: list[Severity] | None = None
    element: str | None = None
    own: bool | None = None
    message: str | None = None
    classification: Classification
    reason: str

    def matches(self, severity: str, element: str, own: bool, message: str) -> bool:
        return ((self.severity is None or severity in self.severity)
                and (self.element is None or fnmatch.fnmatchcase(element.lower(), self.element.lower()))
                and (self.own is None or self.own == own)
                and (self.message is None or re.search(self.message, message, re.IGNORECASE) is not None))


class ErrorRules(BaseModel):
    rules: list[Rule]

    @classmethod
    def load(cls, path: Path) -> 'ErrorRules':
        with path.open(encoding='utf-8') as f:
            data = yaml.safe_load(f)
        return cls(rules=[Rule(classification=r.pop('class'), **r) for r in data['rules']])

    def classify(self, severity: str, element: str, own: bool, message: str) -> tuple[Classification, str]:
        for rule in self.rules:
            if rule.matches(severity, element, own, message):
                return rule.classification, rule.reason
        return 'review', 'No rule matched'


def normalise(message: str) -> str:
    """Collapse the variable parts of a message so repeats of one problem group together."""
    message = re.sub(r'"[^"]*"|\'[^\']*\'', '…', message)
    return re.sub(r'\d+', 'N', message).strip()


def marker(severity: str, element: str, message: str, location: dict[str, Any] | None = None) -> dict[str, Any]:
    return {'severity': _SEVERITY.get(severity, severity), 'element': element, 'message': message,
            'location': location}


def from_stored_error(item: dict[str, Any]) -> dict[str, Any]:
    """A marker from a Stroom StoredError (Error stream MARKER fetch or stepping indicators)."""
    location = item.get('location') or {}
    line, col = location.get('lineNo', -1), location.get('colNo', -1)
    return marker(item.get('severity', 'ERROR'), (item.get('elementId') or {}).get('id', 'unknown'),
                  item.get('message', ''), {'line': line, 'col': col} if line and line > 0 else None)


def triage(markers: list[dict[str, Any]], rules: ErrorRules, own_elements: set[str],
           record_count: int | None = None, examples: int = 3) -> dict[str, Any]:
    """Group markers by (severity, element, normalised message) and classify each group.

    A group that would be benign but occurs at least once per record is raised to review: a
    lookup that fails for every record usually means our output is at fault, not the reference data.
    """
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for m in markers:
        key = (m['severity'], m['element'], normalise(m['message']))
        group = groups.setdefault(key, {'severity': m['severity'], 'element': m['element'],
                                        'own_element': m['element'] in own_elements,
                                        'count': 0, 'examples': [], 'records': set()})
        group['count'] += 1
        if len(group['examples']) < examples and m['message'] not in [e['message'] for e in group['examples']]:
            group['examples'].append({'message': m['message'], 'location': m.get('location')})
        if m.get('record') is not None:
            group['records'].add(m['record'])

    result = []
    for (severity, element, _), group in groups.items():
        classification, reason = rules.classify(severity, element, group['own_element'], group['examples'][0]['message'])
        affected = len(group['records']) or None
        if classification == 'benign' and record_count and (affected or group['count']) >= record_count:
            classification, reason = 'review', 'Occurs for every record; our output may be the cause'
        result.append({'class': classification, 'reason': reason, 'severity': severity, 'element': element,
                       'own_element': group['own_element'], 'count': group['count'],
                       'records_affected': affected, 'examples': group['examples']})
    result.sort(key=lambda g: (_ORDER[g['class']], -g['count']))
    counts = Counter(g['class'] for g in result)
    return {'verdict': 'blocking' if counts['blocking'] else 'review' if counts['review'] else 'clean',
            'groups_by_class': {c: counts.get(c, 0) for c in _ORDER}, 'groups': result}
