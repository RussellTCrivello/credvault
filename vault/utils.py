"""
Parsing of the delimited record lines found in imported data files.

A record line has one known shape::

    URL <sep> email-or-username <sep> password [<sep> IP address]

where ``<sep>`` is ``|``, a tab, ``;`` or ``:``.  The URL comes first, the
email or username second, the password third and the IP address last
(optional).  Every field is checked against its own format and stored in
canonical form (see ``vault.validators``); a line that does not follow the
shape is reported as skipped instead of being stored half-parsed, so the
workspace never fills up with values that only look like a URL, an email or
an IP address.

Because a password may itself contain the separator, the trailing fields are
read from the end: the last field is the IP address only when it really is
one (or an explicit placeholder such as ``N/A``), and everything between the
identifier and the IP address is the password.
"""

import re

from .validators import (
    MAX_HOST_LENGTH, classify_identifier, is_null_ip, normalize_ip,
    normalize_password, normalize_url, url_host,
)


class CanonicalRecord(dict):
    """
    A parsed record whose five values already match their format and are
    already in canonical spelling.

    The importer uses the type to skip validating a line a second time; it
    compares equal to a plain dict with the same keys.
    """

_BOX_CHARS = set("╔╗╚╝║═╠╣█▀▄▌▐░▒▓│─┌┐└┘├┤┬┴┼🔥⚡💵❤️☁")
# Same set as a regex: scanning a line in C beats a Python loop per line.
_BOX_CHARS_RE = re.compile('[' + re.escape(''.join(_BOX_CHARS)) + ']')

# A colon also appears *inside* a URL (after the scheme and before an
# explicit port), so those colons are hidden before a line is split on ':'.
_PROTO_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://")
_PORT_RE = re.compile(r"(__PROTO_COLON__//[^/\s:|;\t]+):(\d+)(?=/)")

#: Separators tried in order; the first one that yields a complete,
#: well-formed record wins, so a ':' inside a URL cannot be mistaken for a
#: separator of a pipe-delimited line.
DELIMITERS = ('|', '\t', ';', ':')

#: What `parse_line` reports when a record carries no IP address.
NO_IP = 'N/A'


def extract_domain(raw_url: str) -> str:
    """
    Extract a clean, lower-case host name from a URL or hostname string.

    Values that are not full URLs (a search term such as ``example`` or a
    half-typed host) fall back to a best-effort guess so that domain
    searches keep working.
    """
    host = url_host(raw_url)
    if host:
        return host.strip('[]').lower()

    target = str(raw_url or '').strip().lower()
    target = target.split('://', 1)[-1]
    for marker in '/?#|;:\t ':
        target = target.split(marker, 1)[0]
    return target[:MAX_HOST_LENGTH]


def _decode(line):
    if isinstance(line, (bytes, bytearray)):
        return line.decode('utf-8', errors='ignore')
    if not isinstance(line, str):
        return str(line)
    return line


def _host_of_canonical_url(url):
    """
    Read the host out of a URL that `normalize_url` has already produced.

    Plain string work on a value known to be well formed -- re-running the
    validator here would double the cost of the import hot path.
    """
    authority = url.split('://', 1)[1]
    for marker in '/?#':
        authority = authority.split(marker, 1)[0]
    if authority.startswith('['):  # an IPv6 literal keeps its brackets
        closing = authority.find(']')
        authority = authority[1:closing] if closing != -1 else authority[1:]
    else:
        host, colon, _port = authority.rpartition(':')
        authority = host if colon else authority
    return authority.lower()


def _split(cleaned, delimiter):
    """
    Split a line on `delimiter`, returning the stripped non-empty fields.

    Returns None when a field in the middle of the line is empty: that means
    `delimiter` is not the separator this line uses (or the line is missing a
    value), and the caller should try the next candidate.
    """
    if delimiter == ':':
        protected = _PROTO_RE.sub(
            lambda m: m.group(0).replace(':', '__PROTO_COLON__'),
            cleaned,
        )
        protected = _PORT_RE.sub(r'\1__PORT_COLON__\2', protected)
    else:
        protected = cleaned

    parts = [
        part.strip()
        .replace('__PROTO_COLON__', ':')
        .replace('__PORT_COLON__', ':')
        for part in protected.split(delimiter)
    ]

    while parts and not parts[0]:
        parts.pop(0)
    while parts and not parts[-1]:
        parts.pop()
    if any(not part for part in parts):
        return None
    return parts


def _split_secret_and_ip(rest, delimiter):
    """
    Split the fields after the identifier into ``(password, ip_address)``.

    The IP address is the last field when -- and only when -- it is a real
    address or an explicit placeholder; everything before it is the
    password, rejoined with the separator so that a separator inside the
    password survives.  Returns ``(None, None)`` when the trailing fields do
    not match the known shape.
    """
    if not rest:
        return None, None

    if len(rest) == 1:
        return normalize_password(rest[0]), None

    tail = rest[-1]
    ip_value = normalize_ip(tail)
    if ip_value is None and not is_null_ip(tail):
        # The trailing field is neither an address nor a placeholder, so the
        # line does not follow the known shape.
        return None, None

    password = normalize_password(delimiter.join(rest[:-1]))
    if password is None:
        return None, None
    return password, ip_value


#: A field of one to five digits may be a URL port (1-65535).
_PORT_TEXT_RE = re.compile(r'^[0-9]{1,5}$')


def _is_explicit_port(text):
    """True when `text` could be a URL port rather than just a number."""
    return bool(_PORT_TEXT_RE.match(text)) and 0 < int(text) < 65536


def _record_from_parts(parts, delimiter):
    """
    Build the canonical record from fields already split on `delimiter`.

    Returns None when any field fails its format check.
    """
    url = normalize_url(parts[0])
    if url is None:
        return None

    account_type, identifier = classify_identifier(parts[1])
    if identifier is None:
        return None

    password, ip_value = _split_secret_and_ip(parts[2:], delimiter)
    if password is None:
        return None

    return CanonicalRecord({
        'domain': _host_of_canonical_url(url),
        'url': url,
        'account_type': account_type,
        'account_identifier': identifier,
        'secret_data': password,
        'ip_address': ip_value or NO_IP,
    })


def _parse_with_delimiter(cleaned, delimiter):
    """Parse `cleaned` as a record using `delimiter`; None when it is not one."""
    parts = _split(cleaned, delimiter)
    if not parts or len(parts) < 3:
        return None

    record = _record_from_parts(parts, delimiter)
    if delimiter == ':' and len(parts) >= 4 and _is_explicit_port(parts[1]):
        # A colon also ends the URL's own `host:port`. When the field after
        # the URL looks like a port, the port belongs to the URL: re-parse
        # with it merged back in so a ported URL without a path is not
        # mistaken for a numeric identifier. If that reading fails, the
        # plain reading still stands, so a numeric username keeps parsing.
        merged = _record_from_parts(
            [parts[0] + ':' + parts[1]] + parts[2:], delimiter,
        )
        if merged is not None:
            return merged
    return record


def parse_line(line):
    """
    Parse a single line of an imported data file.

    Accepts either str or bytes and the ``|``, tab, ``;`` or ``:``
    separators, ignoring decorative borders, blank lines and comments.
    Returns the canonical record dict, or None when the line is not a
    complete record whose five values all match their format.
    """
    cleaned = _decode(line).strip().lstrip('\ufeff')
    if not cleaned or cleaned.startswith('#'):
        return None

    # Filter out ASCII art / border box characters
    if _BOX_CHARS_RE.search(cleaned):
        return None

    for delimiter in DELIMITERS:
        if delimiter not in cleaned:
            continue
        record = _parse_with_delimiter(cleaned, delimiter)
        if record is not None:
            return record

    return None
