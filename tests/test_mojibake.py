from utils.mojibake import repair


def mangle(text: str) -> str:
    """As Windows PowerShell reads a UTF-8 file: its bytes taken as cp1252 (the five it has no character for kept)."""
    return ''.join(bytes([b]).decode('cp1252', errors='ignore') or chr(b) for b in text.encode('utf-8'))


def test_a_round_trip_through_the_windows_code_page_is_put_back():
    # Seen in VS Code: a doc body read back through PowerShell, its dashes and quotes mangled.
    original = "The user’s logon — café, naïve, 25 °C, £5, “quoted”, ✓ and 😀, at 10\u00a0am"
    assert mangle(original) != original
    assert repair(mangle(original)) == (original, 11)
    # Mangled twice over, it is put back twice over.
    assert repair(mangle(mangle('a — b')))[0] == 'a — b'


def test_text_that_isnt_mangled_is_left_as_it_is():
    for text in ("SÃO PAULO", "naïve café — ok", "Ã alone", "plain ASCII <xsl:value-of select=\"a\"/>", "Ð¯"[:1]):
        assert repair(text) == (text, 0)
