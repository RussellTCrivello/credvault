"""explainrejects: classifies rejected lines and never prints a secret."""
import os
import tempfile
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase

from vault.management.commands.explainrejects import (
    classify_rejection, mask_shape,
)


class ExplainRejectsTests(SimpleTestCase):
    def test_reasons(self):
        cases = {
            b'': 'blank_line',
            b'# note': 'comment',
            b'just some words': 'no_separator',
            b'https://a.com|bob': 'only_2_field',
            b'notaurl|bob|pw': 'first_field_not_a_valid_url',
            b'https://a.com|bad@@x|pw': 'second_field_not_email_or_username',
            b'https://a.com|bob|pw|notanip': 'password_or_ip_field_invalid',
        }
        for raw, expected in cases.items():
            self.assertIn(expected, classify_rejection(raw), raw)

    def test_mask_hides_values(self):
        masked = mask_shape('https://Example.com|bob.smith|S3cr3t!Pass')
        for leak in ('Example', 'smith', 'S3cr3t', 'Pass'):
            self.assertNotIn(leak, masked)
        self.assertIn('|', masked)

    def test_command_output_has_no_secret(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'a.txt')
            with open(path, 'wb') as fh:
                fh.write(b'https://ok.com|alice|Pw1\n')
                fh.write(b'garbage-line-with-UniqueSecret42:only\n')
                fh.write(b'https://ok.com|alice|Pw1|UniqueSecret42\n')
            out = StringIO()
            call_command('explainrejects', path, stdout=out)
        text = out.getvalue()
        self.assertIn('1 accepted, 2 rejected', text)
        self.assertNotIn('UniqueSecret42', text)
