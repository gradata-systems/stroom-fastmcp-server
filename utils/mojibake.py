"""Text mangled by a Windows code page round trip, repaired.

Seen in VS Code on Windows: an agent wrote a doc body to a file and read it back with Windows PowerShell, which reads
and writes the ANSI code page (cp1252), so each UTF-8 character came back as two or three (an em dash as 'â€”', a
right quote as 'â€™', é as 'Ã©'). It then spent a stalled terminal command trying to fix them. Text the server is
given is repaired here instead: a run of characters that are the cp1252 reading of one UTF-8 character's bytes is
put back as that character. Anything else is left as it is.
"""
import re

# cp1252's bytes 0x80-0x9F that it has characters for; PowerShell keeps the five it hasn't as C1 controls.
_HIGH = {ch: b for b in range(0x80, 0xA0)
         for ch in [bytes([b]).decode('cp1252', errors='ignore') or chr(b)]}
_CONTINUATION = ''.join(_HIGH) + ''.join(chr(b) for b in range(0xA0, 0xC0))
# A UTF-8 lead byte (two, three or four bytes) read as cp1252, then its continuation bytes read the same way.
_RUN = re.compile(f"[Â-ô][{re.escape(_CONTINUATION)}]{{1,3}}")


def _byte(ch: str) -> int:
    return _HIGH.get(ch, ord(ch))


def repair(text: str) -> tuple[str, int]:
    """The text with mojibake runs put back, and how many were (text mangled twice is put back twice over)."""
    total = 0

    def undo(match: re.Match) -> str:
        nonlocal total
        run = match.group(0)
        try:
            fixed = bytes(_byte(ch) for ch in run).decode('utf-8')
        except UnicodeDecodeError:
            return run
        total += 1
        return fixed
    for _ in range(3):
        before = total
        text = _RUN.sub(undo, text)
        if total == before:
            break
    return text, total


def note(count: int) -> str:
    """What the reply says when text was repaired."""
    return (f"Repaired text mangled by a Windows code page round trip, in {count} place(s) (e.g. 'â€”' back to '—'). "
            f"Pass text to the server directly, not through a file a terminal reads or writes.")
