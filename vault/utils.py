
"""
Parsing helpers shared by the web importer and the ``importfolder``
management command.

``parse_line`` is carried over from the original CLI script with minimal
changes so the accepted file formats stay exactly the same. The streaming
helpers below were added for the bulk-ingest path: they keep memory flat
no matter how large the uploaded file or the scanned folder is.
"""

import codecs
import os
from urllib.parse import urlparse

# Extensions treated as importable text when a whole folder is scanned.
TEXT_EXTENSIONS = frozenset({
    '.txt', '.text', '.csv', '.tsv', '.tab', '.log', '.dat', '.list',
})

# Sentinel used to shield a scheme's colon while a line is split.
_PROTO_GUARD = '\x00'

# Lines are read in fixed-size blocks so a multi-gigabyte file never has to
# be held in memory at once.
READ_CHUNK = 1 << 20


def extract_domain(raw_url: str) -> str:
    """
    Extracts a clean domain name from a URL or hostname string.
    Identical logic to the original script.
    """
    target = (raw_url or '').strip()
    if not target:
        return ''
    if not (target.startswith("http://") or target.startswith("https://")):
        target = "https://" + target
    try:
        parsed = urlparse(target)
        domain = parsed.netloc or parsed.path.split("/")[0]
        domain = domain.split(":")[0].strip("/")
        return domain.lower() if domain else raw_url[:30].lower()
    except Exception:
        return raw_url[:30].lower()


def parse_line(line: str):
    """
    Adaptively parses a single line using colon, pipe, tab,
    or semicolon delimiters while ignoring decorative borders.

    Returns a dict if successful, None otherwise.
    Changed from returning a tuple to returning a dict
    so Django views can work with it more clearly.
    """
    if isinstance(line, bytes):  # defensive: callers should decode first
        line = line.decode('utf-8', errors='ignore')

    cleaned = line.strip()
    if not cleaned or cleaned.startswith("#"):
        return None

    # Filter out ASCII art / border box characters
    box_chars = set("╔╗╚╝║═╠╣█▀▄▌▐░▒▓│─┌┐└┘├┤┬┴┼🔥⚡💵❤️☁")
    if any(c in box_chars for c in cleaned):
        return None

    # Protect the colon of "scheme://" so delimiter detection cannot split a
    # URL in half. A sentinel replace is measurably cheaper than a regex
    # substitution with a callback, and this runs once per imported line.
    protected = cleaned.replace('://', _PROTO_GUARD)

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
        p.strip().replace(_PROTO_GUARD, "://")
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


def iter_decoded_lines(handle, chunk_size=READ_CHUNK, encoding='utf-8-sig'):
    """
    Yield decoded text lines from a *binary* file-like object.

    Django uploads arrive as binary streams, so iterating them directly and
    handing ``bytes`` to ``parse_line`` would fail. Reading in chunks keeps
    memory flat and lets arbitrarily large files be imported.
    """
    decoder = codecs.getincrementaldecoder(encoding)('ignore')
    buffer = ''
    while True:
        chunk = handle.read(chunk_size)
        if not chunk:
            break
        if isinstance(chunk, str):  # already text (e.g. a test double)
            text = chunk
        else:
            text = decoder.decode(chunk)
        # A NUL byte cannot be stored in a text column on every backend and is
        # never part of a real record, so it is dropped on the way in. This
        # also keeps the delimiter guard below unambiguous.
        if '\x00' in text:
            text = text.replace('\x00', '')
        buffer += text
        *lines, buffer = buffer.split('\n')
        for line in lines:
            yield line.rstrip('\r')
    buffer += decoder.decode(b'', True)
    if buffer:
        yield buffer.rstrip('\r')


def parsed_lines(handle, **kwargs):
    """
    Generator of ``parse_line`` results for every line of a binary stream.

    Invalid lines are yielded as ``None`` so the importer can count them.
    """
    for line in iter_decoded_lines(handle, **kwargs):
        if not line.strip():
            continue
        yield parse_line(line)


def looks_like_text(head, max_bytes=1024):
    """Cheap guard used when a whole folder is offered for import."""
    if not head:
        return True
    sample = head[:max_bytes]
    if isinstance(sample, str):
        return '\x00' not in sample
    return b'\x00' not in sample


def is_importable_name(filename, extensions=TEXT_EXTENSIONS,
                       allow_any=False):
    """Whether a file inside an imported folder should be read."""
    if allow_any:
        return True
    return os.path.splitext(filename)[1].lower() in extensions
