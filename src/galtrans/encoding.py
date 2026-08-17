from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DecodedText:
    text: str
    encoding: str
    has_bom: bool


_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def _looks_like_text(text: str) -> bool:
    if not text:
        return True

    disallowed_controls = sum(
        1 for character in text if ord(character) < 32 and character not in "\n\r\t\f"
    )
    return disallowed_controls / len(text) < 0.01


def _decode_without_bom(data: bytes, encoding: str) -> DecodedText | None:
    try:
        text = data.decode(encoding, errors="strict")
    except UnicodeDecodeError:
        return None

    if not _looks_like_text(text):
        return None
    return DecodedText(text=text, encoding=encoding, has_bom=False)


def detect_and_decode(data: bytes) -> DecodedText | None:
    """Decode common visual-novel text encodings without replacing invalid bytes."""
    if not data:
        return DecodedText(text="", encoding="utf-8", has_bom=False)

    for bom, encoding in _BOMS:
        if data.startswith(bom):
            try:
                text = data.decode(encoding, errors="strict")
            except UnicodeDecodeError:
                return None
            if encoding.startswith("utf-16") and text.startswith("\ufeff"):
                text = text[1:]
            return DecodedText(text=text, encoding=encoding, has_bom=True)

    # UTF-16 files occasionally omit a BOM. A high ratio of alternating NUL bytes
    # is a useful conservative signal and avoids treating arbitrary binary files as text.
    if len(data) >= 4:
        even_nuls = data[0::2].count(0)
        odd_nuls = data[1::2].count(0)
        half_length = max(1, len(data) // 2)
        if odd_nuls / half_length > 0.3:
            decoded = _decode_without_bom(data, "utf-16-le")
            if decoded is not None:
                return decoded
        if even_nuls / half_length > 0.3:
            decoded = _decode_without_bom(data, "utf-16-be")
            if decoded is not None:
                return decoded

    for encoding in ("utf-8", "cp932", "gb18030"):
        decoded = _decode_without_bom(data, encoding)
        if decoded is not None:
            return decoded

    return None

