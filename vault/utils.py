
"""
These utility functions are carried over from the original CLI script
with minimal changes. They handle parsing imported data files.
"""

import re
from urllib.parse import urlparse


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


def parse_line(line: str):
    """
    Adaptively parses a single line using colon, pipe, tab, 
    or semicolon delimiters while ignoring decorative borders.
    
    Returns a dict if successful, None otherwise.
    Changed from returning a tuple to returning a dict 
    so Django views can work with it more clearly.
    """
    cleaned = line.strip()
    if not cleaned or cleaned.startswith("#"):
        return None

    # Filter out ASCII art / border box characters
    box_chars = set("╔╗╚╝║═╠╣█▀▄▌▐░▒▓│─┌┐└┘├┤┬┴┼🔥⚡💵❤️☁")
    if any(c in box_chars for c in cleaned):
        return None

    # Protect protocol colons in URLs
    protected = re.sub(
        r"https?://",
        lambda m: m.group(0).replace(":", "__PROTO_COLON__"),
        cleaned,
    )

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
        p.strip().replace("__PROTO_COLON__", ":")
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