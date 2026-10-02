"""Leniency for tool parameters models get slightly wrong.

A list parameter given a single value, e.g. stream_ids=15783601 for one sample stream, failed validation with
"Input should be a valid list", and the model gave up on the tool. Smaller models also send a list as text:
"['Feed']", '["a", "b"]' or "a, b". Clients check arguments against the published schema before the call is
sent, so every list parameter is declared as list | scalar | str (the schema accepts all three) and ONE_OR_MORE
turns what arrives into the list: a single value becomes a list of one, a JSON or Python list literal is
parsed, and a comma-separated string is split.
"""
import ast
import json
from typing import Any

from pydantic import BeforeValidator


def _one_or_more(value: Any) -> Any:
    if value is None or isinstance(value, (list, tuple, set)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith('[') and text.endswith(']'):
            for parse in (json.loads, ast.literal_eval):
                try:
                    parsed = parse(text)
                except (ValueError, SyntaxError):
                    continue
                if isinstance(parsed, (list, tuple)):
                    return list(parsed)
        if text.startswith('{') and text.endswith('}'):
            try:
                return [json.loads(text)]
            except ValueError:
                pass
        if ',' in text and '{' not in text:
            return [part.strip() for part in text.split(',') if part.strip()]
        return [text] if text else []
    return [value]


ONE_OR_MORE = BeforeValidator(_one_or_more)
