from provendoc.core.foundation.char_policy import (
    ANSI_ESCAPE_PATTERN,
    HIDDEN_UNICODE_MAP,
)


def detect_hidden_unicode(text: str) -> list[dict]:
    """Detect zero-width and bidi control characters, return [{type, count}, ...].

    The character set and names are declared by the core.foundation.char_policy
    table (single source of truth).
    """
    hidden_chars = []
    for char, name in HIDDEN_UNICODE_MAP.items():
        count = text.count(char)
        if count > 0:
            hidden_chars.append({
                "type": name,
                "count": count
            })
    return hidden_chars


def contains_ansi_escape(text: str) -> bool:
    """Whether an ANSI escape sequence (\\x1B[...m pattern) is present."""
    return bool(ANSI_ESCAPE_PATTERN.search(text))
