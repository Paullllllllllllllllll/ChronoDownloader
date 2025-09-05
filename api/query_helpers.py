import re


def escape_sru_literal(value: str) -> str:
    """Escape a literal for inclusion in SRU/CQL quoted phrases.

    - Escapes backslashes and double quotes.
    - Collapses newlines and tabs into spaces.
    """
    if value is None:
        return ""
    s = str(value)
    s = s.replace("\\", r"\\")
    s = s.replace('"', r'\"')
    s = re.sub(r"[\r\n\t]+", " ", s)
    return s


def escape_sparql_string(value: str) -> str:
    """Escape a string for safe inclusion in SPARQL single-quoted literals."""
    if value is None:
        return ""
    s = str(value)
    s = s.replace("\\", r"\\")
    s = s.replace("'", r"\'")
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return s
