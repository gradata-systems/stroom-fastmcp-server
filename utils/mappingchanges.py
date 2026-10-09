"""Changes to a translation mapping, merged into the whole one: a fix sends only what it changes.

Seen (Gemma 4 31B in VS Code, about 22 tokens a second): each fix resent the whole mapping, some 800 tokens and 40
seconds of writing, five times over. A rule is replaced whole by name, a common entry by its path (and Data name),
and any other key whole; {"remove": true} removes a rule or a common entry.
"""
import copy
from typing import Any

from fastmcp.exceptions import ToolError


def _common_key(entry: dict[str, Any]) -> tuple[str, str]:
    return str(entry.get('path')), str(entry.get('data_name') or '')


def _said(key: tuple[str, str]) -> str:
    return f"common '{key[0]}'" + (f" (Data '{key[1]}')" if key[1] else '')


def apply_changes(base: dict[str, Any], changes: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The mapping with the changes merged in, and what each change did."""
    if not isinstance(changes, dict) or not changes:
        raise ToolError("changes is an object holding only what changes, e.g. {\"events\": [{the rule, whole}]} or "
                        "{\"common\": [{\"path\": ..., \"value\": ...}]}")
    merged, done = copy.deepcopy(base), []
    for key, value in changes.items():
        if key == 'events':
            rules = merged.setdefault('events', [])
            for rule in value if isinstance(value, list) else [value]:
                if not isinstance(rule, dict) or not rule.get('name'):
                    raise ToolError("Each rule in changes.events needs its name: a rule replaces the one of that "
                                    "name whole, or is added after the others")
                at = next((i for i, r in enumerate(rules) if r.get('name') == rule['name']), None)
                if rule.get('remove'):
                    if at is None:
                        raise ToolError(f"changes.events removes rule '{rule['name']}', which the mapping does not "
                                        f"have (its rules: {[r.get('name') for r in rules]})")
                    rules.pop(at)
                    done.append(f"rule '{rule['name']}' removed")
                elif at is None:
                    rules.append(rule)
                    done.append(f"rule '{rule['name']}' added after the others")
                else:
                    rules[at] = rule
                    done.append(f"rule '{rule['name']}' replaced")
        elif key == 'common':
            entries = merged.setdefault('common', [])
            for entry in value if isinstance(value, list) else [value]:
                if not isinstance(entry, dict) or not entry.get('path'):
                    raise ToolError("Each entry in changes.common needs its path (and data_name for a Data entry): "
                                    "it replaces the entry with that path, or is added")
                found = _common_key(entry)
                at = next((i for i, e in enumerate(entries) if _common_key(e) == found), None)
                if entry.get('remove'):
                    if at is None:
                        raise ToolError(f"changes.common removes {_said(found)}, which the mapping does not have")
                    entries.pop(at)
                    done.append(f"{_said(found)} removed")
                elif at is None:
                    entries.append(entry)
                    done.append(f"{_said(found)} added")
                else:
                    entries[at] = entry
                    done.append(f"{_said(found)} replaced")
        else:
            merged[key] = value
            done.append(f"'{key}' replaced")
    return merged, done
