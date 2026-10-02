"""What counts as sample data.

A client hands the model file attachments as references; a model that does not read them passes the paths on,
and the server then profiles, uploads and plans around a Windows path. The server cannot read the client's files,
so a sample that is a path, a list of paths or an attachment reference is refused with the instruction a model
can act on: pass the file's text.
"""
import re
from typing import Any

from fastmcp.exceptions import ToolError

_PATH = re.compile(r'^(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\|/(?:[\w.-]+/)+|~/|file://|#file:|#attachment:)[^\n]*$')
_REFERENCE = re.compile(r'^#(?:file|attachment):\S+$')


def path_like(token: str) -> bool:
    token = token.strip().strip('"\'')
    return bool(token) and (bool(_PATH.match(token)) or bool(_REFERENCE.match(token)))


def why_not_data(sample: str, name: str = 'sample') -> str | None:
    """Why the text is not sample data: empty, a file path or list of paths, or an attachment reference."""
    text = (sample or '').strip()
    if not text:
        return f"{name} is empty: pass the file's text"
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


def as_named_samples(samples: Any, single: str | None = None) -> dict[str, str]:
    """Samples as {name: text}, from a dict by file name, a list of texts, or one text; each checked."""
    if isinstance(samples, dict):
        named = {str(k): v for k, v in samples.items()}
    elif isinstance(samples, list):
        named = {f'sample {n}': v for n, v in enumerate(samples, 1)}
    elif isinstance(samples, str):
        named = {'sample': samples}
    else:
        named = {}
    if single:
        named.setdefault('sample', single)
    for name, text in named.items():
        if not isinstance(text, str):
            raise ToolError(f"{name}: a sample is the file's text, not {type(text).__name__}")
        check_sample(text, name)
    return named
