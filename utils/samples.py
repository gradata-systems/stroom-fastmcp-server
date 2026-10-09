"""What counts as sample data.

A client hands the model file attachments as references; a model that does not read them passes the paths on,
and the server then profiles, uploads and plans around a Windows path. The server cannot read the client's files,
so a sample that is a path, a list of paths or an attachment reference is refused with the instruction a model
can act on: pass the file's text.

Small models behind a local tool-call parser also send the text broken up: a list holding a {name: text} object, or
(Gemma 4 on vLLM) the file's text cut at ':' and ',' into the keys and values of an object. The published type
admits those shapes, so the client's schema check does not reject them with a bare "must be string" the model
retries unchanged; the server then reads what it can and says plainly what arrived broken.
"""
import re
from typing import Any

from fastmcp.exceptions import ToolError

# The published type of a samples argument: by file name, a list of texts (or {name: text} objects), or one text.
SampleTexts = dict[str, str] | list[str | dict[str, str]] | str

_PATH = re.compile(r'^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\|/(?:[\w.-]+/)+|~/|file://|#file:|#attachment:)[^\n]*$')
_REFERENCE = re.compile(r'^#(?:file|attachment):\S+$')


def path_like(token: str) -> bool:
    token = token.strip().strip('"\'')
    return bool(token) and (bool(_PATH.match(token)) or bool(_REFERENCE.match(token)))


# What a file reader leaves where it cut a file short: VS Code's read_file ends a long line with
# '[... truncated at 2000 characters]'.
_CUT = re.compile(r'truncated at \d[\d,]* (?:characters|chars|bytes|lines)|\[\.\.\.\s*(?:\d[\d,]* (?:more )?(?:characters|lines|bytes)|truncated)', re.I)


def why_not_data(sample: str, name: str = 'sample') -> str | None:
    """Why the text is not sample data: empty, a file path or list of paths, an attachment reference, or a file
    a reader cut short."""
    text = (sample or '').strip()
    if not text:
        return f"{name} is empty: pass the file's text"
    cut = _CUT.search(text)
    if cut:
        return (f"{name} holds a file reader's cut ({cut.group(0)!r}): you didn't get the whole file, so what was passed "
                f"isn't its data. Don't trim, complete or repair it (an agent completed a cut record with values it "
                f"made up). Send the files with upload_sample files=[their paths] (a command per file for the user's terminal: it goes "
                f"from their disk to Stroom whole, not through you); tools then take its stream_ids.")
    tokens = [t for t in re.split(r'[,\n;]+', text) if t.strip()]
    if tokens and len(tokens) <= 50 and all(path_like(t) for t in tokens):
        shown = tokens[0].strip()[:80]
        return (f"{name} is a file path or attachment reference ({shown!r}), not the file's content. This server cannot "
                f"read the client's files: read the file and pass its text (every line of it) as the sample.")
    return None


def check_sample(sample: str, name: str = 'sample') -> None:
    reason = why_not_data(sample, name)
    if reason:
        raise ToolError(reason)


def _not_a_name(key: str) -> bool:
    """A key no file name has: more than one line (or an escaped newline), quotes, or the length of a record."""
    return '\n' in key or '\\n' in key or '"' in key or len(key) > 255


def why_broken(named: dict[str, Any]) -> str | None:
    """Why {name: text} is not file names and texts but one file's text cut into pieces by the tool call."""
    odd = [k for k in named if _not_a_name(k)]
    if not odd:
        return None
    return (f"samples arrived broken: the tool call cut the file's text into the keys and values of an object (keys "
            f"such as {odd[0][:60]!r}), most likely at the ':' and ',' in it. The data is fine; the arguments are not. "
            f"Call again with samples as a list of strings, each one file's whole text as a single JSON string "
            f"(newlines escaped as \\n, quotes as \\\"), not an object.")


def _unescape(text: str) -> str:
    """A text sent with its newlines escaped twice (literal \\n, no line breaks) has them restored."""
    if '\n' not in text.strip() and text.count('\\n') >= 2:
        return text.replace('\\r\\n', '\n').replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"')
    return text


_NAME_KEYS = ('name', 'file', 'filename', 'file_name', 'path')
_TEXT_KEYS = ('text', 'content', 'contents', 'data', 'sample')


def _record_of_one_file(item: dict) -> tuple[str, Any] | None:
    """(name, text) from {name: ..., text: ...}, the shape models give one file; None for {file name: text}."""
    keys = {str(k).lower(): k for k in item}
    text_key = next((keys[k] for k in _TEXT_KEYS if k in keys), None)
    if text_key is None or set(keys) - set(_NAME_KEYS) - set(_TEXT_KEYS):
        return None
    name_key = next((keys[k] for k in _NAME_KEYS if k in keys), None)
    return (str(item[name_key]) if name_key else ''), item[text_key]


def as_named_samples(samples: Any, single: str | None = None) -> dict[str, str]:
    """Samples as {name: text}, from a dict by file name, a list of texts or {name: text} objects, or one text;
    each checked. An object that is one file's text cut into pieces is refused with what to send instead."""
    if isinstance(samples, dict):
        named = {str(k): v for k, v in samples.items()}
    elif isinstance(samples, list):
        named = {}
        for n, item in enumerate(samples, 1):
            if isinstance(item, dict) and _record_of_one_file(item):
                name, text = _record_of_one_file(item)
                named[name or f'sample {n}'] = text
            elif isinstance(item, dict):
                named.update({str(k): v for k, v in item.items()})
            else:
                named[f'sample {n}'] = item
    elif isinstance(samples, str):
        named = {'sample': samples}
    else:
        named = {}
    if single:
        named.setdefault('sample', single)
    broken = why_broken(named)
    if broken:
        raise ToolError(broken)
    for name, text in named.items():
        if not isinstance(text, str):
            raise ToolError(f"{name}: a sample is the file's text, not {type(text).__name__}")
        check_sample(text, name)
        named[name] = _unescape(text)
    return named


# How much sample text a model should send: enough records to tell the format and fields, no more. The model writes
# it out a token at a time (seen: Gemma 4 31B at ~22 tokens a second took over 75 s for 4,400 characters, and read 100
# lines, 60 KB, it would have taken over ten minutes to send).
SAMPLE_GUIDE = ("about 10 records and at most 8,000 characters a file: you write this text out a token at a time, so "
                "more only costs time. Files on the user's disk are better given as files= (their paths): their text "
                "then never passes through you")
_LONG = 20_000


def long_samples_note(named: dict[str, str]) -> str | None:
    """A note for next time when the texts sent were far more than profiling needs."""
    long = {name: len(text) for name, text in named.items() if len(text) > _LONG}
    if not long:
        return None
    said = ', '.join(f"{name} ({size:,} characters)" for name, size in long.items())
    return (f"More sample text than profiling needs: {said}. About 10 records a file is enough; for files on the "
            f"user's disk give their paths (start_onboarding files=), so the text never passes through you.")
