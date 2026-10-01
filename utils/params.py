"""Leniency for tool parameters models get slightly wrong.

A list parameter given a single value, e.g. stream_ids=15783601 for one sample stream, failed validation with
"Input should be a valid list", which clients show as invalid input, and the model gave up on the tool. The
schema still says array; ONE_OR_MORE accepts the single value too, as a list of one.
"""
from typing import Any

from pydantic import BeforeValidator


def _one_or_more(value: Any) -> Any:
    return value if value is None or isinstance(value, (list, tuple, set)) else [value]


ONE_OR_MORE = BeforeValidator(_one_or_more)
