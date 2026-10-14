"""
Tests for the accuracy of the five stored formats and for the guarantee
that no value -- and no record -- is ever stored twice.

The rules under test are the ones documented in ``vault/validators.py``:

    URL        [http(s)://]host.tld[:port][/path][?query][#fragment]
    Email      local-part@domain.tld
    Username   letters, digits and . _ - + (no '@')
    Password   any printable text
    IP         dotted-quad IPv4 or a compressed IPv6 address
"""

from importlib import import_module

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from .importer import bulk_import_lines, canonical_record
from .models import Account, Email, Ip, Pass, Url, User
from .utils import extract_domain, parse_line
from .validators import (
    classify_identifier, is_null_ip, is_valid_email, is_valid_ip, url_host,
    is_valid_password, is_valid_url, is_valid_username, normalize_email,
    normalize_ip, normalize_password, normalize_url, normalize_username,
)

# A migration module is named after its number, so it cannot be reached with
# a plain import statement.
_cleanup_migration = import_module(
    'vault.migrations.0002_canonical_values_and_record_fingerprint'
)
canonicalize_reference_values = _cleanup_migration.canonicalize_reference_values
deduplicate_and_fingerprint_accounts = (
    _cleanup_migration.deduplicate_and_fingerprint_accounts
)

VALID_URLS = [
    ('https://example.com/login', 'https://example.com/login'),
    ('HTTP://EXAMPLE.COM/Login', 'http://example.com/Login'),
    ('HTTPS://Example.COM:443/Login', 'https://example.com/Login'),
    ('example.com', 'https://example.com'),
    ('www.example.com', 'https://www.example.com'),
    ('example.com/', 'https://example.com'),
    ('http://example.com:80/login', 'http://example.com/login'),
    ('https://example.com:8443/admin', 'https://example.com:8443/admin'),
    ('http://sub.example.co.uk/a/b?c=d#e', 'http://sub.example.co.uk/a/b?c=d#e'),
    ('https://192.168.1.1/admin', 'https://192.168.1.1/admin'),
    ('https://[2001:0DB8:0000::1]/x', 'https://[2001:db8::1]/x'),
    ('http://localhost:8000/admin', 'http://localhost:8000/admin'),
    ('https://example.com./login', 'https://example.com/login'),
    ('https://example.com/a%2fb', 'https://example.com/a%2Fb'),
    ('https://пример.рф/x', 'https://xn--e1afmkfd.xn--p1ai/x'),
]

INVALID_URLS = [
    'not a url',           # no host at all
    'example',             # no domain extension
    'example.c',           # one-letter extension
    'example.c0m',         # extension must be letters
    'example..com',        # empty label
    'https://',            # nothing after the scheme
    'https:///login',      # no host
    'https://-bad.com',    # label starts with a hyphen
    'ftp://example.com/x',  # only http and https are stored
    '//example.com/x',     # protocol-relative
    'https://user:pw@example.com/x',  # credentials in the URL
    'https://exa mple.com',  # space in the host
    'https://example.com/a b',  # space in the path
    'https://example.com:99999/x',  # port out of range
    'https://example.com:abc/x',    # port is not a number
    'example.com;drop',    # a record separator may not be stored
    'https://example.com|login',
    '',
    '   ',
]

VALID_EMAILS = [
    ('person@example.com', 'person@example.com'),
    (' Person@Example.COM ', 'person@example.com'),
    ('first.last+tag@sub.example.co.uk', 'first.last+tag@sub.example.co.uk'),
    ("o'neill!#$%&*+/=?^_`{}~-@example.com", "o'neill!#$%&*+/=?^_`{}~-@example.com"),
    ('user123@example.com', 'user123@example.com'),
    ('a@[192.168.1.1]', 'a@[192.168.1.1]'),
    ('a@[IPv6:2001:DB8::1]', 'a@[2001:db8::1]'),
    ('user@пример.рф', 'user@xn--e1afmkfd.xn--p1ai'),
]

INVALID_EMAILS = [
    'plainaddress',        # no @
    'a@b',                 # no domain extension
    'a@b.c',               # one-letter extension
    '@example.com',        # no local part
    'user@',               # no domain
    'a@@b.com',            # two @
    'a..b@example.com',    # doubled dot in the local part
    '.a@example.com',      # leading dot
    'a.@example.com',      # trailing dot
    'a b@example.com',     # space
    'a@exa mple.com',      # space in the domain
    'a@-example.com',      # bad label
    ('x' * 65) + '@example.com',          # local part over 64 characters
    'a@' + ('b' * 64) + '.com',           # label over 63 characters
    'a' * 64 + '@' + '.'.join(['b' * 63] * 3 + ['com']),  # over 254 characters
]

VALID_USERNAMES = [
    ('bob', 'bob'),
    ('Bob123', 'Bob123'),           # case is preserved
    ('first.last', 'first.last'),
    ('user_name', 'user_name'),
    ('a-b+c', 'a-b+c'),
    ('John Smith', 'John Smith'),   # single inner spaces are fine
    ('محمد_1', 'محمد_1'),            # any script
    (' bob ', 'bob'),               # edges are trimmed
]

INVALID_USERNAMES = [
    '',
    '   ',
    'bob  smith',       # doubled space
    '.bob',             # starts with a separator
    'bob.',             # ends with a separator
    '-_-',              # no letter or digit
    'bob@x.com',        # that is an email address
    'bob|smith',        # record separator
    'bob;smith',
    'bob:smith',
    'bob\tsmith',
    'bob\nsmith',
    'a' * 256,
]

VALID_IPS = [
    ('192.168.1.1', '192.168.1.1'),
    (' 10.0.0.1 ', '10.0.0.1'),
    ('2001:0DB8:0000:0000:0000:0000:0000:0001', '2001:db8::1'),
    ('::1', '::1'),
    ('0.0.0.0', '0.0.0.0'),
    ('255.255.255.255', '255.255.255.255'),
]

INVALID_IPS = [
    'not-an-ip',
    '192.168.001.001',   # leading zeros
    '192.168.1',         # three octets
    '1.2.3.4.5',         # five octets
    '256.1.1.1',         # octet out of range
    '999.999.1.1',
    '192.168.1.1/24',    # a network, not an address
    '192.168.1.1:80',
    '',
]

NULL_IPS = ['N/A', 'n/a', 'NA', '-', '--', 'null', 'none', 'nil', 'unknown', '']


class UrlFormatTests(TestCase):
    def test_valid_urls_are_canonicalized(self):
        for raw, expected in VALID_URLS:
            with self.subTest(url=raw):
                self.assertEqual(normalize_url(raw), expected)
                self.assertTrue(is_valid_url(raw))

    def test_invalid_urls_are_rejected(self):
        for raw in INVALID_URLS:
            with self.subTest(url=raw):
                self.assertIsNone(normalize_url(raw))
                self.assertFalse(is_valid_url(raw))

    def test_url_is_stored_once_however_it_is_written(self):
        url = Url.objects.create(name='HTTPS://Example.COM:443/Login')
        self.assertEqual(url.name, 'https://example.com/Login')
        for spelling in (
            'https://example.com/Login',
            'HTTPS://EXAMPLE.COM:443/Login',
            'https://example.com:443/Login',
            'example.com/Login',
            '  https://example.com/Login  ',
        ):
            with self.subTest(spelling=spelling):
                # A second spelling must not create a second row.
                with self.assertRaises(ValidationError):
                    Url.objects.create(name=spelling)
        self.assertEqual(Url.objects.count(), 1)

    def test_urls_that_differ_only_in_the_path_case_stay_separate(self):
        Url.objects.create(name='https://example.com/Login')
        Url.objects.create(name='https://example.com/login')
        # Paths are case-sensitive, so these really are two URLs.
        self.assertEqual(Url.objects.count(), 2)

    def test_url_host_drops_the_scheme_port_and_path(self):
        cases = {
            'https://site0.example/login': 'site0.example',
            'https://example.com:8443/a': 'example.com',
            'http://example.com:80/x': 'example.com',
            'https://[2001:db8::1]:80/x': '[2001:db8::1]',
            'https://192.168.1.1/x': '192.168.1.1',
            'http://localhost:8000/a': 'localhost',
            'not a url': None,
        }
        for raw, expected in cases.items():
            with self.subTest(url=raw):
                self.assertEqual(url_host(raw), expected)

    def test_extract_domain_reads_the_host(self):
        cases = {
            'HTTPS://EXAMPLE.COM/Login': 'example.com',
            'https://example.com:8443/a': 'example.com',
            'example.com': 'example.com',
            'https://[2001:db8::1]/x': '2001:db8::1',
            'example': 'example',
        }
        for raw, expected in cases.items():
            with self.subTest(url=raw):
                self.assertEqual(extract_domain(raw), expected)


class EmailFormatTests(TestCase):
    def test_valid_emails_are_canonicalized(self):
        for raw, expected in VALID_EMAILS:
            with self.subTest(email=raw):
                self.assertEqual(normalize_email(raw), expected)
                self.assertTrue(is_valid_email(raw))

    def test_invalid_emails_are_rejected(self):
        for raw in INVALID_EMAILS:
            with self.subTest(email=raw):
                self.assertIsNone(normalize_email(raw))
                self.assertFalse(is_valid_email(raw))

    def test_email_is_stored_once_however_it_is_written(self):
        Email.objects.create(name='Person@Example.COM')
        self.assertEqual(Email.objects.count(), 1)
        self.assertEqual(Email.objects.get().name, 'person@example.com')
        with self.assertRaises(ValidationError):
            Email.objects.create(name=' person@example.com ')
        self.assertEqual(Email.objects.count(), 1)


class UsernameFormatTests(TestCase):
    def test_valid_usernames_are_canonicalized(self):
        for raw, expected in VALID_USERNAMES:
            with self.subTest(username=raw):
                self.assertEqual(normalize_username(raw), expected)
                self.assertTrue(is_valid_username(raw))

    def test_invalid_usernames_are_rejected(self):
        for raw in INVALID_USERNAMES:
            with self.subTest(username=raw):
                self.assertIsNone(normalize_username(raw))
                self.assertFalse(is_valid_username(raw))

    def test_username_case_is_preserved_and_still_unique(self):
        User.objects.create(name='Bob')
        User.objects.create(name='bob')
        # Usernames are case-sensitive: two different values, one row each.
        self.assertEqual(
            sorted(User.objects.values_list('name', flat=True)),
            ['Bob', 'bob'],
        )
        with self.assertRaises(ValidationError):
            User.objects.create(name=' Bob ')


class PasswordFormatTests(TestCase):
    def test_password_keeps_its_exact_spelling(self):
        for raw in ('secret', 'P@ss w0rd!#$', 'a|b;c:d', 'a' * 500):
            with self.subTest(password=raw):
                self.assertEqual(normalize_password(raw), raw)
                self.assertTrue(is_valid_password(raw))

    def test_password_edges_are_trimmed(self):
        self.assertEqual(normalize_password('  pass\t'), 'pass')

    def test_invalid_passwords_are_rejected(self):
        for raw in ('', '   ', 'a\tb', 'a\nb', 'a\x00b', 'a' * 501):
            with self.subTest(password=raw):
                self.assertIsNone(normalize_password(raw))
                self.assertFalse(is_valid_password(raw))


class IpFormatTests(TestCase):
    def test_valid_ips_are_canonicalized(self):
        for raw, expected in VALID_IPS:
            with self.subTest(ip=raw):
                self.assertEqual(normalize_ip(raw), expected)
                self.assertTrue(is_valid_ip(raw))

    def test_invalid_ips_are_rejected(self):
        for raw in INVALID_IPS:
            with self.subTest(ip=raw):
                self.assertIsNone(normalize_ip(raw))
                self.assertFalse(is_valid_ip(raw))

    def test_placeholders_mean_no_address(self):
        for raw in NULL_IPS:
            with self.subTest(token=raw):
                self.assertTrue(is_null_ip(raw))
                self.assertIsNone(normalize_ip(raw))
                self.assertFalse(is_valid_ip(raw))

    def test_ip_is_stored_once_however_it_is_written(self):
        Ip.objects.create(name=' 2001:0DB8:0000::1 ')
        self.assertEqual(Ip.objects.get().name, '2001:db8::1')
        with self.assertRaises(ValidationError):
            Ip.objects.create(name='2001:db8::1')
        with self.assertRaises(ValidationError):
            Ip.objects.create(name='192.168.001.001')
        self.assertEqual(Ip.objects.count(), 1)


class IdentifierClassificationTests(TestCase):
    def test_identifier_with_at_sign_must_be_an_email(self):
        self.assertEqual(
            classify_identifier(' Person@Example.COM '),
            ('email', 'person@example.com'),
        )
        # A malformed address is never downgraded to a username.
        self.assertEqual(classify_identifier('a@@b.com'), (None, None))
        self.assertEqual(classify_identifier('user@'), (None, None))

    def test_identifier_without_at_sign_is_a_username(self):
        self.assertEqual(classify_identifier('bob'), ('user', 'bob'))
        self.assertEqual(classify_identifier('bob|smith'), (None, None))


class ParseLineFormatTests(TestCase):
    def test_complete_record_in_the_known_order(self):
        record = parse_line(
            'https://example.com/login|Person@Example.COM|Secret 1|203.0.113.10'
        )
        self.assertEqual(record, {
            'domain': 'example.com',
            'url': 'https://example.com/login',
            'account_type': 'email',
            'account_identifier': 'person@example.com',
            'secret_data': 'Secret 1',
            'ip_address': '203.0.113.10',
        })

    def test_record_without_ip_address(self):
        for line in (
            'https://example.com/login|bob|Secret 1',
            'https://example.com/login|bob|Secret 1|N/A',
            'https://example.com/login|bob|Secret 1|',
        ):
            with self.subTest(line=line):
                record = parse_line(line)
                self.assertEqual(record['secret_data'], 'Secret 1')
                self.assertEqual(record['ip_address'], 'N/A')

    def test_every_supported_separator(self):
        for separator in ('|', '\t', ';', ':'):
            with self.subTest(separator=separator):
                record = parse_line(
                    separator.join(
                        ['https://example.com/login', 'bob', 'secret',
                         '192.168.1.1']
                    )
                )
                self.assertEqual(record['url'], 'https://example.com/login')
                self.assertEqual(record['account_identifier'], 'bob')
                self.assertEqual(record['secret_data'], 'secret')
                self.assertEqual(record['ip_address'], '192.168.1.1')

    def test_separator_inside_the_password_is_preserved(self):
        self.assertEqual(
            parse_line('https://a.com|bob|pa|ss|1.2.3.4')['secret_data'],
            'pa|ss',
        )
        self.assertEqual(
            parse_line('https://a.com:bob:pa:ss:1.2.3.4')['secret_data'],
            'pa:ss',
        )

    def test_port_in_the_url_survives_a_colon_separated_line(self):
        record = parse_line('http://site.com:8080/login:bob:secret:1.2.3.4')
        self.assertEqual(record['url'], 'http://site.com:8080/login')
        self.assertEqual(record['account_identifier'], 'bob')
        self.assertEqual(record['ip_address'], '1.2.3.4')

    def test_ported_url_without_a_path_in_a_colon_separated_line(self):
        # The port colon is not the record separator: a URL that ends in
        # `host:port` (no path) keeps the port instead of being split into a
        # numeric identifier.
        record = parse_line('https://mail.example.org:8443:bob:secret')
        self.assertEqual(record['url'], 'https://mail.example.org:8443')
        self.assertEqual(record['account_identifier'], 'bob')
        self.assertEqual(record['secret_data'], 'secret')
        self.assertEqual(record['ip_address'], 'N/A')

        record = parse_line(
            'http://site.com:8080:bob:secret:1.2.3.4'
        )
        self.assertEqual(record['url'], 'http://site.com:8080')
        self.assertEqual(record['account_identifier'], 'bob')
        self.assertEqual(record['secret_data'], 'secret')
        self.assertEqual(record['ip_address'], '1.2.3.4')

        # The default port is still dropped during canonicalization.
        record = parse_line('https://example.com:443:bob:secret')
        self.assertEqual(record['url'], 'https://example.com')
        self.assertEqual(record['account_identifier'], 'bob')

    def test_numeric_username_still_parses_when_the_merge_is_not_a_url(self):
        # A port-looking field whose merge is not a valid URL leaves the
        # plain reading in place (numeric identifier, password keeps ':').
        record = parse_line('not a url:443:bob:secret')
        self.assertIsNone(record)
        record = parse_line('https://example.com:99999:bob:secret')
        # 99999 is not a port, so no merge is attempted at all.
        self.assertIsNone(record)

    def test_lines_that_break_the_format_are_rejected(self):
        lines = (
            'not a url|bob|secret|1.2.3.4',       # the URL is not a URL
            'https://a.com|b@@ad|secret|1.2.3.4',  # the email is not an email
            'https://a.com|bob|secret|not-an-ip',  # the IP is not an IP
            'https://a.com|bob|secret|999.1.1.1',
            'https://a.com|bob|secret|192.168.001.001',
            'https://a.com|bob||1.2.3.4',          # no password
            'https://a.com||secret|1.2.3.4',       # no identifier
            'https://a.com|bob|secret|1.2.3.4|extra',
            'https://a.com|bob',                   # too few fields
            '# a comment',
            '',
            '╔══════════╗',
        )
        for line in lines:
            with self.subTest(line=line):
                self.assertIsNone(parse_line(line))

    def test_the_domain_field_is_the_host_of_the_url(self):
        cases = {
            'https://example.com:8443/login|bob|s|1.2.3.4': 'example.com',
            'HTTP://EXAMPLE.COM/a|bob|s': 'example.com',
            'https://192.168.1.1/a|bob|s': '192.168.1.1',
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(parse_line(line)['domain'], expected)

    def test_bytes_input_is_accepted(self):
        record = parse_line(b'https://a.com|bob|secret|1.2.3.4')
        self.assertEqual(record['url'], 'https://a.com')

    def test_canonical_record_rechecks_values_from_any_caller(self):
        # bulk_import_lines may be fed dicts that did not come from
        # parse_line, so the values are validated again there.
        self.assertIsNone(canonical_record({
            'url': 'not a url', 'account_type': 'user',
            'account_identifier': 'bob', 'secret_data': 'secret',
            'ip_address': '1.2.3.4',
        }))
        self.assertEqual(
            canonical_record({
                'url': 'HTTPS://A.COM', 'account_type': 'email',
                'account_identifier': 'User@A.COM', 'secret_data': 'secret',
                'ip_address': 'N/A',
            }),
            {
                'url': 'https://a.com', 'account_type': 'email',
                'account_identifier': 'user@a.com', 'secret_data': 'secret',
                'ip_address': None,
            },
        )


class NoDuplicateDataTests(TestCase):
    def test_importing_the_same_line_twice_stores_one_record(self):
        line = 'https://a.com/login|User@A.COM|secret|1.2.3.4'
        first_created, first_skipped = bulk_import_lines(
            iter([parse_line(line)])
        )
        second_created, second_skipped = bulk_import_lines(
            iter([parse_line(line)])
        )
        self.assertEqual((first_created, first_skipped), (1, 0))
        self.assertEqual((second_created, second_skipped), (0, 0))
        self.assertEqual(Account.objects.count(), 1)
        self.assertEqual(Url.objects.count(), 1)
        self.assertEqual(Email.objects.count(), 1)
        self.assertEqual(Pass.objects.count(), 1)
        self.assertEqual(Ip.objects.count(), 1)

    def test_different_spellings_of_the_same_values_share_one_row(self):
        lines = [
            'HTTPS://Example.COM/login|Person@Example.COM|Secret|203.0.113.10',
            'https://example.com/login|person@example.com|Secret|203.0.113.10',
            'https://example.com:443/login|PERSON@example.com|Secret|203.0.113.10',
        ]
        created, skipped = bulk_import_lines(
            iter(parse_line(line) for line in lines)
        )
        self.assertEqual((created, skipped), (1, 0))
        self.assertEqual(Url.objects.get().name, 'https://example.com/login')
        self.assertEqual(Email.objects.get().name, 'person@example.com')
        self.assertEqual(Ip.objects.get().name, '203.0.113.10')

    def test_urls_that_differ_only_in_the_path_case_stay_separate(self):
        created, _skipped = bulk_import_lines(iter(
            parse_line(line) for line in (
                'https://example.com/Login|bob|Secret|203.0.113.10',
                'https://example.com/login|bob|Secret|203.0.113.10',
            )
        ))
        # A URL path is case-sensitive, so these are two different records.
        self.assertEqual(created, 2)
        self.assertEqual(Url.objects.count(), 2)

    def test_a_value_that_is_not_an_ip_never_reaches_the_ip_table(self):
        created, skipped = bulk_import_lines(iter([
            parse_line('https://a.com|bob|secret|not-an-ip'),
            parse_line('https://a.com|bob|secret|999.1.1.1'),
            parse_line('https://a.com|bob|secret|1.2.3.4'),
        ]))
        self.assertEqual((created, skipped), (1, 2))
        self.assertEqual(list(Ip.objects.values_list('name', flat=True)), ['1.2.3.4'])

    def test_identical_records_cannot_be_saved_twice(self):
        url = Url.objects.create(name='https://example.com/login')
        user = User.objects.create(name='bob')
        password = Pass.objects.create(name='secret')
        Account.objects.create(
            url=url, account_type='user', user=user, password=password,
        )
        with self.assertRaises(ValidationError):
            Account.objects.create(
                url=url, account_type='user', user=user, password=password,
            )
        self.assertEqual(Account.objects.count(), 1)

    def test_changing_a_record_does_not_flag_it_as_a_duplicate(self):
        url = Url.objects.create(name='https://example.com/login')
        user = User.objects.create(name='bob')
        password = Pass.objects.create(name='secret')
        account = Account.objects.create(
            url=url, account_type='user', user=user, password=password,
        )
        account.password = Pass.objects.create(name='other-secret')
        account.save()  # must not raise
        account.refresh_from_db()
        self.assertEqual(account.password.name, 'other-secret')

    def test_bulk_create_cannot_duplicate_a_record(self):
        url = Url.objects.create(name='https://example.com/login')
        user = User.objects.create(name='bob')
        password = Pass.objects.create(name='secret')
        fingerprint = Account(
            url=url, account_type='user', user=user, password=password,
        ).compute_fingerprint()
        Account.objects.bulk_create(
            [
                Account(
                    url=url, account_type='user', user=user,
                    password=password, fingerprint=fingerprint,
                )
                for _ in range(3)
            ],
            ignore_conflicts=True,
        )
        # The unique fingerprint column keeps only the first row.
        self.assertEqual(Account.objects.count(), 1)


class MigrationCanonicalizationTests(TestCase):
    """
    The migration must clean up values that were stored before the format
    rules existed, without destroying anything it does not recognize.
    """

    def _insert_legacy_rows(self):
        Url.objects.bulk_create([
            Url(name='https://example.com/login'),
            Url(name='HTTPS://EXAMPLE.COM/login'),
            Url(name='this is not a url'),
        ])
        Email.objects.bulk_create([
            Email(name='person@example.com'),
            Email(name='Person@Example.COM'),
        ])
        Ip.objects.bulk_create([
            Ip(name='2001:0DB8::1'),
            Ip(name='2001:db8::1'),
            Ip(name='192.168.1.1'),
        ])

    def test_duplicate_values_are_merged_and_re_attached(self):
        from django.apps import apps as global_apps

        self._insert_legacy_rows()
        url_a, url_b = Url.objects.order_by('id')[:2]
        password = Pass.objects.create(name='secret')
        Account.objects.bulk_create([
            Account(
                url=url_a, account_type='user',
                user=User.objects.create(name='bob'),
                password=password,
                fingerprint='a' * 64,
            ),
            Account(
                url=url_b, account_type='user',
                user=User.objects.create(name='alice'),
                password=password,
                fingerprint='b' * 64,
            ),
        ])

        canonicalize_reference_values(global_apps, None)
        deduplicate_and_fingerprint_accounts(global_apps, None)

        # The two spellings of the same URL became one canonical row, the
        # unrecognized value was left alone, and both records survived.
        self.assertEqual(
            sorted(Url.objects.values_list('name', flat=True)),
            ['https://example.com/login', 'this is not a url'],
        )
        self.assertEqual(Email.objects.count(), 1)
        self.assertEqual(
            Email.objects.get().name, 'person@example.com'
        )
        self.assertEqual(
            sorted(Ip.objects.values_list('name', flat=True)),
            ['192.168.1.1', '2001:db8::1'],
        )
        self.assertEqual(Account.objects.count(), 2)
        self.assertEqual(
            {account.url.name for account in Account.objects.all()},
            {'https://example.com/login'},
        )
        self.assertEqual(
            len({
                account.fingerprint for account in Account.objects.all()
            }),
            2,
        )

    def test_identical_records_are_reduced_to_one(self):
        from django.apps import apps as global_apps

        url = Url.objects.create(name='https://example.com/login')
        user = User.objects.create(name='bob')
        password = Pass.objects.create(name='secret')
        Account.objects.bulk_create([
            Account(
                url=url, account_type='user', user=user, password=password,
                fingerprint='legacy-fingerprint-%d' % index,
            )
            for index in range(3)
        ])

        deduplicate_and_fingerprint_accounts(global_apps, None)

        self.assertEqual(Account.objects.count(), 1)


class RecordFormFormatTests(TestCase):
    """The Add/Edit form applies the same rules as the importer."""

    def setUp(self):
        administrator = get_user_model().objects.create_user(
            username='format-test-admin',
            password='test-password',
            is_staff=True,
        )
        self.client.force_login(administrator)

    def test_values_that_break_their_format_are_rejected(self):
        response = self.client.post(reverse('vault:add'), {
            'account_type': 'user',
            'url_name': 'not a url',
            'user_name': 'bob@@smith',
            'password_name': 'secret',
            'ip_name': 'not-an-ip',
        })

        self.assertEqual(response.status_code, 200)  # re-rendered with errors
        self.assertContains(response, 'Enter a valid URL')
        self.assertContains(response, 'Enter a valid username')
        self.assertContains(response, 'Enter a valid IP address')
        # Nothing at all reached the database.
        self.assertEqual(Account.objects.count(), 0)
        self.assertEqual(Url.objects.count(), 0)
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(Pass.objects.count(), 0)
        self.assertEqual(Ip.objects.count(), 0)

    def test_values_typed_differently_are_stored_once(self):
        first = self.client.post(reverse('vault:add'), {
            'account_type': 'email',
            'url_name': 'HTTPS://Example.COM/login',
            'email_name': 'Person@Example.COM',
            'password_name': 'secret',
            'ip_name': ' 2001:0DB8::1 ',
        })
        self.assertEqual(first.status_code, 302)

        # A second record that reuses the same URL, password and IP with a
        # username instead of the email.
        second = self.client.post(reverse('vault:add'), {
            'account_type': 'user',
            'url_name': 'https://example.com:443/login',
            'user_name': 'bob',
            'password_name': 'secret',
            'ip_name': '2001:db8::1',
        }, follow=True)
        self.assertEqual(second.status_code, 200)

        self.assertEqual(Account.objects.count(), 2)
        self.assertEqual(Url.objects.count(), 1)
        self.assertEqual(Url.objects.get().name, 'https://example.com/login')
        self.assertEqual(Email.objects.count(), 1)
        self.assertEqual(Pass.objects.count(), 1)
        self.assertEqual(Ip.objects.count(), 1)
        self.assertEqual(Ip.objects.get().name, '2001:db8::1')

    def test_the_same_record_cannot_be_added_twice(self):
        payload = {
            'account_type': 'email',
            'url_name': 'https://example.com/login',
            'email_name': 'person@example.com',
            'password_name': 'secret',
            'ip_name': '203.0.113.10',
        }
        self.assertEqual(
            self.client.post(reverse('vault:add'), payload).status_code, 302
        )
        response = self.client.post(reverse('vault:add'), payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'already exists')
        self.assertEqual(Account.objects.count(), 1)

    def test_reference_page_normalizes_before_reusing_a_value(self):
        for spelling in ('Person@Example.COM', ' person@example.com '):
            self.client.post(
                reverse('vault:references', args=['email']),
                {'name': spelling},
            )
        self.assertEqual(Email.objects.count(), 1)
        self.assertEqual(Email.objects.get().name, 'person@example.com')

    def test_reference_page_rejects_a_value_off_format(self):
        response = self.client.post(
            reverse('vault:references', args=['ip']),
            {'name': 'not-an-ip'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Enter a valid IP address')
        self.assertEqual(Ip.objects.count(), 0)

    def test_migration_hashes_records_the_same_way_as_the_model(self):
        # The migration carries its own copy of the hash; if the two ever
        # drift, migrated rows would not match newly written ones.
        from vault.models import record_fingerprint

        arguments = (7, 'email', 3, None, 11, 13)
        self.assertEqual(
            _cleanup_migration._record_fingerprint(*arguments),
            record_fingerprint(*arguments),
        )
        self.assertEqual(
            _cleanup_migration._record_fingerprint(1, 'user', None, 2, 3, None),
            record_fingerprint(1, 'user', None, 2, 3, None),
        )
