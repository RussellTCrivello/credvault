"""
Strict recognition and canonical spelling of the five formats the vault
stores.  Every value that enters the workspace -- typed on the Add/Edit
Record form, added on a reference page, or read from an imported file --
passes through this module first.

The five formats and what is accepted
=====================================

URL
    ``[http:// | https://] host [.tld] [:port] [/path] [?query] [#fragment]``

    * The scheme is optional; when it is missing the value is stored as
      ``https://``.  Only ``http`` and ``https`` are accepted.
    * The host must be a real host name: labels of letters, digits and
      hyphens separated by dots, ending in an alphabetic TLD of at least
      two characters (``example.com``, ``sub.example.co.uk``).  A single
      label is only accepted for ``localhost``.  An IPv4 address or a
      bracketed IPv6 literal is also a valid host, and an
      internationalized name is stored in punycode
      (``https://مثال.إدارة`` -> ``https://xn--mgbh0fb.xn--kgbegwl``).
    * A port, when present, must be 1-65535.  The default port of the
      scheme (80/443) is dropped.
    * The path, query and fragment are kept as typed, except that
      ``%xx`` escapes are upper-cased.
    * Spaces, control characters, quotes, ``<>``, ``\\``, ``^``, ``|`` and
      ``;`` are rejected: they are either not allowed in a URL or would not
      survive a round trip through a delimited file.

    ``HTTPS://Example.COM:443/Login`` and ``example.com/Login`` are both
    stored as ``https://example.com/Login`` -- one row, not two.

Email
    ``local-part @ domain``

    * The local part is 1-64 characters of
      ``A-Z a-z 0-9 ! # $ % & ' * + / = ? ^ _ ` { } ~ -`` with dots
      allowed between atoms (never first, last or doubled).
    * The domain is a host name with a TLD, or an ``[IPv4]`` /
      ``[IPv6:...]`` literal.
    * The whole address is at most 254 characters and exactly one ``@``
      is allowed.
    * Addresses are stored lower-cased, so ``Person@Example.COM`` and
      ``person@example.com`` are the same row.

Username
    A normal name: letters (any script), digits and ``. _ - +``, with
    single spaces allowed inside.  It must start and end with a letter or
    a digit, contain at least one letter or digit, be at most 255
    characters, and may not contain ``@`` (that makes it an email),
    whitespace runs, control characters or the record delimiters
    ``| ; : tab``.  The spelling is preserved exactly -- usernames are
    case-sensitive, so ``Bob`` and ``bob`` are different values.

Password
    Any printable text, 1-500 characters, with surrounding whitespace,
    byte-order marks and zero-width characters removed and control
    characters rejected.  The spelling is preserved exactly -- passwords
    are case-sensitive.

IP address
    Dotted-quad IPv4 (``192.168.1.1``) or any IPv6 address.  Every octet
    must be 0-255 with no leading zeros, so ``192.168.001.001``,
    ``192.168.1`` and ``999.1.1.1`` are rejected.  Addresses are stored
    in their canonical compressed form, so ``2001:0DB8:0000::1`` becomes
    ``2001:db8::1``.  The placeholders ``N/A``, ``-``, ``null``, ``none``,
    ``nil`` and ``unknown`` (the full list is ``NULL_IP_TOKENS``) mean "no
    address" and are stored as an empty relation rather than as a value
    that only looks like an address.

Why canonicalization is what prevents duplicates
================================================
Each reference table has ``unique=True`` on its name column, so a value
can only be stored twice if the two spellings differ.  Normalizing before
the lookup makes the spellings identical, which turns "the same URL,
email, username, password or IP" into one row no matter how it was
written in the source file.

Public API
==========
For every kind there are three entry points::

    is_valid_<kind>(value)   -> bool          cheap True/False test
    normalize_<kind>(value)  -> str | None    canonical spelling, or None
    validate_<kind>(value)   -> str           canonical spelling, or raises
                                              ``django.core.exceptions.ValidationError``

plus ``classify_identifier(value)`` which decides whether a value is an
email or a username, ``<KIND>FormatValidator`` classes that plug straight
into Django form and model fields, and the ``CANONICALIZERS`` /
``VALIDATORS`` registries keyed by the reference-table kind
(``url``, ``email``, ``user``, ``pass``, ``ip``).
"""

import ipaddress
import re
import unicodedata

from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible
from django.utils.translation import gettext_lazy as _

# ---------------------------------------------------------------------------
# Limits -- kept identical to the model field lengths in vault/models.py.
# ---------------------------------------------------------------------------
MAX_URL_LENGTH = 2048
# PostgreSQL refuses a B-tree index entry above about 2700 bytes, and the URL
# is indexed twice (the unique name and its lower-cased copy). Non-ASCII path
# characters take several bytes each, so a URL of 2048 characters can exceed
# that limit. URLs are therefore capped at 2000 UTF-8 bytes, measured both as
# stored and lower-cased. Plain ASCII URLs are never affected by this cap.
MAX_URL_BYTES = 2000
MAX_HOST_LENGTH = 253
MAX_LABEL_LENGTH = 63
MAX_EMAIL_LENGTH = 254
MAX_EMAIL_LOCAL_LENGTH = 64
MAX_USERNAME_LENGTH = 255
MAX_PASSWORD_LENGTH = 500
MAX_IP_LENGTH = 45

DEFAULT_URL_SCHEME = 'https'
ALLOWED_URL_SCHEMES = ('http', 'https')
DEFAULT_PORTS = {'http': 80, 'https': 443}

#: Tokens a source file may use in the IP column instead of a real address.
NULL_IP_TOKENS = frozenset({
    '', '-', '--', '/', '.', '?', 'n/a', 'na', 'n.a.', 'none', 'null',
    'nil', 'unknown',
})

#: Characters that may never appear in a stored URL: control characters,
#: whitespace, the characters RFC 3986 calls "unwise", and the delimiters a
#: record line is split on (a stored value must survive a round trip).
_INVALID_URL_CHARS_RE = re.compile(r'[\x00-\x20\x7f-\x9f"<>\\^`{|};]')

_SCHEME_RE = re.compile(r'^([A-Za-z][A-Za-z0-9+.\-]*)://')
_PERCENT_ESCAPE_RE = re.compile(r'%([0-9A-Fa-f]{2})')
_LABEL_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')
# Dotted quad, matched against the whole value (see normalize_ip).
_IPV4_RE = re.compile(r'(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\Z')

# RFC 5322 "atext" minus ':' ';' and '|', which would break the record
# delimiters used by imported files.
_ATEXT = r"A-Za-z0-9!#$%&'*+/=?^_`{}~-"
_EMAIL_LOCAL_RE = re.compile(
    r'^[' + _ATEXT + r']+(?:\.[' + _ATEXT + r']+)*$'
)

# A username is a normal name: letters (any script), digits, underscore and
# the separators below, with single spaces allowed inside.
_USERNAME_RE = re.compile(r'^(?:\w|[ .\-+])+$')
_USERNAME_EDGE_CHARS = ' .+-_'

# Leading/trailing noise that is stripped from every stored value.
_EDGE_NOISE = '\ufeff\u200b\u200c\u200d \t\r\n\v\f'

# ---------------------------------------------------------------------------
# Error messages (translatable; attached to the offending form field).
# ---------------------------------------------------------------------------
URL_ERROR = _(
    'Enter a valid URL: http:// or https://, then a host name with a '
    'domain extension such as example.com.'
)
EMAIL_ERROR = _(
    'Enter a valid email address in the form name@domain.com '
    '(letters, digits and . _ + - before the @).'
)
USERNAME_ERROR = _(
    'Enter a valid username: letters, digits and . _ - + only, starting '
    'and ending with a letter or digit, and without @.'
)
PASSWORD_ERROR = _(
    'Enter a password of 1 to 500 printable characters.'
)
IP_ERROR = _(
    'Enter a valid IP address such as 192.168.1.1 '
    '(four numbers of 0-255 separated by dots), or leave it blank.'
)

ERROR_MESSAGES = {
    'url': URL_ERROR,
    'email': EMAIL_ERROR,
    'user': USERNAME_ERROR,
    'pass': PASSWORD_ERROR,
    'ip': IP_ERROR,
}

#: Example values shown next to each form field.
FORMAT_HINTS = {
    'url': _('Format: https://example.com/login'),
    'email': _('Format: name@domain.com'),
    'user': _('Format: name, name123 or first.last'),
    'pass': _('Format: any printable text, up to 500 characters'),
    'ip': _('Format: 192.168.1.1 (or N/A)'),
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _as_text(value):
    """Return `value` as str, or None when there is nothing to work with."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode('utf-8', errors='ignore')
    elif not isinstance(value, str):
        value = str(value)
    return value.strip(_EDGE_NOISE) or None


def _idna_encode(host):
    """Punycode an internationalized host name; None when it is not valid."""
    if host.isascii():
        return host
    try:
        return host.encode('idna').decode('ascii')
    except (UnicodeError, UnicodeDecodeError):
        return None


def normalize_host(value):
    """
    Return the canonical, lower-case, ASCII form of a host name.

    Accepts ``example.com``, ``sub.example.co.uk.``, an internationalized
    name (converted to punycode), a bare IPv4 address or a bracketed IPv6
    literal.  Returns None when the value is not a usable host.
    """
    host = _as_text(value)
    if host is None:
        return None
    host = host.lower()
    if host.endswith('.'):  # a trailing dot is the DNS root, not a label
        host = host[:-1]
    if not host or len(host) > MAX_HOST_LENGTH:
        return None

    if host.startswith('[') and host.endswith(']'):
        return _normalize_ipv6_literal(host[1:-1])

    # A bare IPv4 or IPv6 address is a perfectly good host.  The probe is
    # skipped for ordinary names: only a literal can start with a digit or
    # contain a colon, and the parse is the slowest check in this function.
    if host[0].isdigit() or ':' in host:
        try:
            return str(ipaddress.ip_address(host))
        except ValueError:
            pass

    if host == 'localhost':
        return host

    # The domain extension must be letters only; it is checked on the
    # original spelling because a punycoded TLD ('xn--kgbegwl') is not.
    extension = host.rpartition('.')[2]
    if not 2 <= len(extension) <= MAX_LABEL_LENGTH or not extension.isalpha():
        return None

    # Internationalized names are converted next so that the label checks
    # below run against the ASCII form that is actually stored.
    ascii_host = _idna_encode(host)
    if ascii_host is None or len(ascii_host) > MAX_HOST_LENGTH:
        return None

    labels = ascii_host.split('.')
    if len(labels) < 2:  # no domain extension -> not a public host name
        return None
    for label in labels:
        if not label or len(label) > MAX_LABEL_LENGTH:
            return None
        if not _LABEL_RE.match(label):
            return None
    return ascii_host


def _normalize_ipv6_literal(text):
    """Canonicalize the inside of an ``[...]`` IPv6 literal."""
    text = (text or '').strip()
    if not text:
        return None
    try:
        return '[' + str(ipaddress.IPv6Address(text)) + ']'
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# URL
# ---------------------------------------------------------------------------
def normalize_url(value):
    """
    Return the canonical spelling of a URL, or None when `value` is not one.

    ``'EXAMPLE.COM/Login'`` -> ``'https://example.com/Login'``
    ``'HTTPS://Example.COM:443/Login'`` -> ``'https://example.com/Login'``
    """
    raw = _as_text(value)
    if raw is None or len(raw) > MAX_URL_LENGTH:
        return None
    if _INVALID_URL_CHARS_RE.search(raw):
        return None
    if raw.startswith('//'):  # protocol-relative: no scheme to canonicalize
        return None

    match = _SCHEME_RE.match(raw)
    if match:
        scheme = match.group(1).lower()
        if scheme not in ALLOWED_URL_SCHEMES:
            return None
        remainder = raw[match.end():]
    else:
        scheme = DEFAULT_URL_SCHEME
        remainder = raw

    if not remainder or remainder[0] in '/?#':
        return None  # 'https:///path' has no host

    cut = len(remainder)
    for marker in '/?#':
        found = remainder.find(marker)
        if found != -1:
            cut = min(cut, found)
    authority, tail = remainder[:cut], remainder[cut:]

    if '@' in authority:  # user:pass@host credentials are never stored
        return None

    port = None
    if authority.startswith('['):
        closing = authority.find(']')
        if closing == -1:
            return None
        host = _normalize_ipv6_literal(authority[1:closing])
        if host is None:
            return None
        after, colon, port_text = authority[closing + 1:].partition(':')
        if after:  # anything other than ':port' may not follow the literal
            return None
    else:
        host_text, colon, port_text = authority.rpartition(':')
        if not colon:
            host_text, port_text = authority, ''
        host = normalize_host(host_text)
        if host is None:
            return None

    if port_text:
        if not port_text.isdigit() or len(port_text) > 5:
            return None
        port = int(port_text)
        if not 0 < port < 65536:
            return None
        if DEFAULT_PORTS.get(scheme) == port:
            port = None  # the scheme already implies it

    tail = _PERCENT_ESCAPE_RE.sub(lambda m: '%' + m.group(1).upper(), tail)
    if tail.endswith('#'):
        tail = tail[:-1]
    if tail.endswith('?'):
        tail = tail[:-1]
    if tail == '/':
        tail = ''  # 'https://example.com/' and 'https://example.com' match

    url = f'{scheme}://{host}' + (f':{port}' if port else '') + tail
    if not 0 < len(url) <= MAX_URL_LENGTH:
        return None
    if (len(url.encode('utf-8')) > MAX_URL_BYTES
            or len(url.lower().encode('utf-8')) > MAX_URL_BYTES):
        return None  # too large for a database index (see MAX_URL_BYTES)
    return url


def is_valid_url(value):
    """True when `value` is a well-formed http(s) URL or bare host."""
    return normalize_url(value) is not None


def validate_url(value):
    """Canonical URL, or ``ValidationError``."""
    url = normalize_url(value)
    if url is None:
        raise ValidationError(URL_ERROR, code='invalid_url')
    return url


def url_host(value):
    """The host part of a URL (bracketed for IPv6, without the port), or None."""
    url = normalize_url(value)
    if url is None:
        return None
    authority = url.split('://', 1)[1]
    for marker in '/?#':
        authority = authority.split(marker, 1)[0]
    if authority.startswith('['):  # an IPv6 literal keeps its brackets
        closing = authority.find(']')
        return authority[:closing + 1] if closing != -1 else None
    host, colon, _port = authority.rpartition(':')
    return host if colon else authority  # drop the ':port' part


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def _normalize_email_domain(domain):
    domain = (domain or '').strip()
    if not domain:
        return None
    if domain.startswith('[') and domain.endswith(']'):
        inner = domain[1:-1]
        if inner[:5].lower() == 'ipv6:':
            inner = inner[5:]
        try:
            return '[' + str(ipaddress.ip_address(inner)) + ']'
        except ValueError:
            return None
    return normalize_host(domain)


def normalize_email(value):
    """
    Return the canonical lower-case spelling of an email address, or None.

    ``' Person@Example.COM '`` -> ``'person@example.com'``
    """
    raw = _as_text(value)
    if raw is None or len(raw) > MAX_EMAIL_LENGTH:
        return None

    local, separator, domain = raw.rpartition('@')
    if not separator or not local or not domain:
        return None
    if '@' in local:  # exactly one '@' is allowed
        return None
    if len(local) > MAX_EMAIL_LOCAL_LENGTH:
        return None
    if not _EMAIL_LOCAL_RE.match(local):
        return None

    host = _normalize_email_domain(domain)
    if host is None:
        return None

    email = f'{local.lower()}@{host}'
    return email if len(email) <= MAX_EMAIL_LENGTH else None


def is_valid_email(value):
    """True when `value` is a well-formed email address."""
    return normalize_email(value) is not None


def validate_email(value):
    """Canonical email address, or ``ValidationError``."""
    email = normalize_email(value)
    if email is None:
        raise ValidationError(EMAIL_ERROR, code='invalid_email')
    return email


# ---------------------------------------------------------------------------
# Username
# ---------------------------------------------------------------------------
def normalize_username(value):
    """
    Return the canonical spelling of a username, or None.

    The spelling is preserved (usernames are case-sensitive); only
    surrounding whitespace and zero-width characters are removed.
    """
    raw = _as_text(value)
    if raw is None or len(raw) > MAX_USERNAME_LENGTH:
        return None
    if '@' in raw:  # that is an email address, not a username
        return None
    if not _USERNAME_RE.match(raw):
        return None
    if '  ' in raw:
        return None
    if raw[0] in _USERNAME_EDGE_CHARS or raw[-1] in _USERNAME_EDGE_CHARS:
        return None
    if not any(char.isalnum() for char in raw):
        return None
    return raw


def is_valid_username(value):
    """True when `value` is a well-formed username."""
    return normalize_username(value) is not None


def validate_username(value):
    """Canonical username, or ``ValidationError``."""
    username = normalize_username(value)
    if username is None:
        raise ValidationError(USERNAME_ERROR, code='invalid_username')
    return username


# ---------------------------------------------------------------------------
# Password
# ---------------------------------------------------------------------------
def normalize_password(value):
    """
    Return the canonical spelling of a password, or None.

    Only the edges are touched: surrounding whitespace, byte-order marks and
    zero-width characters are removed.  The body is preserved exactly,
    including its case and any punctuation.
    """
    if isinstance(value, (bytes, bytearray)):
        value = value.decode('utf-8', errors='ignore')
    elif value is None:
        return None
    elif not isinstance(value, str):
        value = str(value)

    raw = value.strip(_EDGE_NOISE)
    if not raw or len(raw) > MAX_PASSWORD_LENGTH:
        return None
    for char in raw:
        if unicodedata.category(char) == 'Cc':  # control characters
            return None
    return raw


def is_valid_password(value):
    """True when `value` can be stored as a password."""
    return normalize_password(value) is not None


def validate_password(value):
    """Canonical password, or ``ValidationError``."""
    password = normalize_password(value)
    if password is None:
        raise ValidationError(PASSWORD_ERROR, code='invalid_password')
    return password


# ---------------------------------------------------------------------------
# IP address
# ---------------------------------------------------------------------------
def is_null_ip(value):
    """True when `value` is a placeholder meaning "no IP address"."""
    raw = _as_text(value)
    return raw is None or raw.lower() in NULL_IP_TOKENS


def normalize_ip(value):
    """
    Return the canonical compressed form of an IP address, or None.

    Placeholders such as ``N/A`` also return None -- they mean the record
    has no address, which is stored as an empty relation rather than as a
    value that only looks like an address.
    """
    raw = _as_text(value)
    if raw is None or raw.lower() in NULL_IP_TOKENS:
        return None
    if len(raw) > MAX_IP_LENGTH:
        return None

    # Fast path for the dotted quad, which is by far the most common case:
    # leading zeros and octets over 255 are rejected, and anything else is
    # already the canonical spelling.
    match = _IPV4_RE.match(raw)
    if match:
        octets = match.groups()
        for octet in octets:
            if len(octet) > 1 and octet[0] == '0':
                return None  # '010.0.0.1' is ambiguous and not canonical
            if int(octet) > 255:
                return None
        return raw

    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return None


def is_valid_ip(value):
    """True when `value` is a real IPv4 or IPv6 address."""
    return normalize_ip(value) is not None


def validate_ip(value):
    """Canonical IP address, or ``ValidationError``."""
    ip_address = normalize_ip(value)
    if ip_address is None:
        raise ValidationError(IP_ERROR, code='invalid_ip')
    return ip_address


def validate_optional_ip(value):
    """Canonical IP address, ``None`` for a placeholder, else ValidationError."""
    if is_null_ip(value):
        return None
    return validate_ip(value)


# ---------------------------------------------------------------------------
# Identifier classification (the field between the URL and the password)
# ---------------------------------------------------------------------------
def classify_identifier(value):
    """
    Decide whether `value` is an email address or a username.

    Returns ``(kind, canonical_value)`` where kind is ``'email'`` or
    ``'user'``, or ``(None, None)`` when the value matches neither format.
    A value containing ``@`` must be a valid email: it is never silently
    downgraded to a username, because ``user@@x`` is bad data and storing
    it as a username would only hide the problem.
    """
    raw = _as_text(value)
    if raw is None:
        return None, None
    if '@' in raw:
        email = normalize_email(raw)
        return ('email', email) if email else (None, None)
    username = normalize_username(raw)
    return ('user', username) if username else (None, None)


# ---------------------------------------------------------------------------
# Registries and Django field validators
# ---------------------------------------------------------------------------
CANONICALIZERS = {
    'url': normalize_url,
    'email': normalize_email,
    'user': normalize_username,
    'pass': normalize_password,
    'ip': normalize_ip,
}

VALIDATORS = {
    'url': validate_url,
    'email': validate_email,
    'user': validate_username,
    'pass': validate_password,
    'ip': validate_ip,
}

MAX_LENGTHS = {
    'url': MAX_URL_LENGTH,
    'email': MAX_EMAIL_LENGTH,
    'user': MAX_USERNAME_LENGTH,
    'pass': MAX_PASSWORD_LENGTH,
    'ip': MAX_IP_LENGTH,
}


def canonicalize(kind, value):
    """Canonical spelling of `value` for a reference kind, or None."""
    return CANONICALIZERS[kind](value)


def is_valid(kind, value):
    """True when `value` matches the format of the given reference kind."""
    return CANONICALIZERS[kind](value) is not None


@deconstructible
class FormatValidator:
    """
    Base validator: canonicalizes a value and rejects it when the format
    does not match.  Subclasses only set ``kind``; the message and the
    error code are derived from it, which keeps migrations stable.
    """

    kind = None

    def __call__(self, value):
        canonical = CANONICALIZERS[self.kind](value)
        if canonical is None:
            raise ValidationError(
                ERROR_MESSAGES[self.kind], code=f'invalid_{self.kind}'
            )
        return canonical

    def __eq__(self, other):
        return (
            isinstance(other, FormatValidator)
            and self.__class__ is other.__class__
            and self.kind == other.kind
        )

    def __hash__(self):
        return hash((self.__class__.__name__, self.kind))


class URLFormatValidator(FormatValidator):
    kind = 'url'


class EmailFormatValidator(FormatValidator):
    kind = 'email'


class UsernameFormatValidator(FormatValidator):
    kind = 'user'


class PasswordFormatValidator(FormatValidator):
    kind = 'pass'


class IPAddressFormatValidator(FormatValidator):
    kind = 'ip'

    def __call__(self, value):
        if is_null_ip(value):
            return None
        return super().__call__(value)


FIELD_VALIDATORS = {
    'url': URLFormatValidator,
    'email': EmailFormatValidator,
    'user': UsernameFormatValidator,
    'pass': PasswordFormatValidator,
    'ip': IPAddressFormatValidator,
}
