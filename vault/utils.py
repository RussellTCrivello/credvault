"""
These utility functions are carried over from the original CLI script
with minimal changes. They handle parsing imported data files.
"""

import re
from urllib.parse import urlparse

_BOX_CHARS = set("╔╗╚╝║═╠╣█▀▄▌▐░▒▓│─┌┐└┘├┤┬┴┼🔥⚡💵❤️☁")
_PROTO_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://")
_PORT_RE = re.compile(r"(__PROTO_COLON__//[^/\s:|;\t]+):(\d+)(?=/)")


def extract_domain(raw_url: str) -> str:
    """
    Extracts a clean domain name from a URL or hostname string.
    Identical logic to the original script.
    """
    target = raw_url.strip()
    if not (target.startswith("http://") or target.startswith("https://")):
        target = "https://" + target
    try:
        parsed = urlparse(target)
        domain = parsed.netloc or parsed.path.split("/")[0]
        domain = domain.split(":")[0].strip("/")
        return domain.lower() if domain else raw_url[:30]
    except Exception:
        return raw_url[:30]


def parse_line(line):
    """
    Adaptively parses a single line using colon, pipe, tab,
    or semicolon delimiters while ignoring decorative borders.

    Accepts either str or bytes. Returns a dict if the line meets
    all record requirements, or None otherwise.
    """
    if isinstance(line, (bytes, bytearray)):
        line = line.decode("utf-8", errors="ignore")
    elif not isinstance(line, str):
        line = str(line)

    cleaned = line.strip().lstrip("\ufeff")
    if not cleaned or cleaned.startswith("#"):
        return None

    # Filter out ASCII art / border box characters
    if any(c in _BOX_CHARS for c in cleaned):
        return None

    # Protect protocol colons and URL port colons
    protected = _PROTO_RE.sub(
        lambda m: m.group(0).replace(":", "__PROTO_COLON__"),
        cleaned,
    )
    protected = _PORT_RE.sub(r"\1__PORT_COLON__\2", protected)

    # Detect delimiter
    delimiter = None
    for d in ["|", "\t", ";", ":"]:
        if d in protected:
            parts = protected.split(d)
            if len(parts) >= 3:
                delimiter = d
                break

    if not delimiter:
        return None

    parts = [
        p.strip()
        .replace("__PROTO_COLON__", ":")
        .replace("__PORT_COLON__", ":")
        for p in protected.split(delimiter)
    ]
    parts = [p for p in parts if p != ""]

    if len(parts) < 3:
        return None

    raw_url = parts[0]

    if len(parts) == 3:
        identifier, secret, ip_addr = parts[1], parts[2], "N/A"
    elif len(parts) == 4:
        identifier, secret, ip_addr = parts[1], parts[2], parts[3]
    else:
        identifier = parts[1]
        secret = ":".join(parts[2:])
        ip_addr = "N/A"

    if not (raw_url and identifier and secret):
        return None

    if len(raw_url) > 2048 or len(identifier) > 255 or len(secret) > 500:
        return None

    if len(ip_addr) > 45:
        ip_addr = "N/A"

    domain = extract_domain(raw_url)
    account_type = "email" if "@" in identifier else "user"

    return {
        'domain': domain,
        'url': raw_url,
        'account_type': account_type,
        'account_identifier': identifier,
        'secret_data': secret,
        'ip_address': ip_addr,
    }
