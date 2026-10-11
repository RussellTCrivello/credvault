# Data formats

The workspace stores five kinds of value -- URL, email, username, password and
IP address. Every value that enters it, whether typed on the Add/Edit Record
form, added on a reference page, or read from an imported file, is checked
against the format of its kind and stored in one canonical spelling. That is
what makes the two rules of this project hold:

* **Accuracy** -- a value that does not match its format is refused (the form
  shows why, an imported line is counted as skipped) instead of being stored
  half-parsed.
* **No duplicates** -- because a value is always stored in the same spelling,
  the unique index on each reference table collapses every spelling of the
  same value onto one row, and the record fingerprint does the same for whole
  records.

| Kind | Format | Stored as |
| --- | --- | --- |
| URL | `[http:// \| https://] host.extension [:port] [/path] [?query] [#fragment]` -- the scheme is optional and defaults to `https://` | scheme and host lower-cased, default port (`80`/`443`) dropped, `%xx` escapes upper-cased, no trailing `/` on a bare origin, IDN hosts in punycode |
| Email | `name@domain.extension` -- letters, digits and `. _ + - ! # $ % & ' * / = ? ^ \` { } ~` before the `@`, dots between atoms only | fully lower-cased |
| Username | letters (any script), digits and `. _ - +`, single spaces inside, starting and ending with a letter or digit, no `@` | exactly as typed (case matters) |
| Password | any printable text, 1-500 characters | exactly as typed, edges trimmed |
| IP address | four numbers of 0-255 separated by dots (`192.168.1.1`), or an IPv6 address | canonical compressed form; `N/A`, `-`, `null`, `none`, `nil` and `unknown` mean "no address" |

Rejected examples: `not a url`, `example` (no extension), `example.c`,
`ftp://example.com`, `a@@b.com`, `user@`, `a..b@example.com`, `a@b.c`,
`.bob`, `bob@x.com` (as a username), `192.168.1`, `192.168.001.001`,
`999.1.1.1`, `192.168.1.1/24`.

## Imported files

One record per line, in this order:

```
URL <sep> email-or-username <sep> password [<sep> IP address]
```

`<sep>` is `|`, a tab, `;` or `:`. The identifier is an email when it holds a
valid address, otherwise a username. The IP address is optional; because a
password may contain the separator, the last field is treated as the address
only when it really is one. Blank lines, `#` comments and decorative box
characters are ignored.

```
https://example.com/login|person@example.com|Secret 1|203.0.113.10
https://example.com/login;bob;Secret 2;N/A
https://example.com/login:bob:Secret:3:1.2.3.4
```

The rules live in `vault/validators.py`, which is the single place to change
if a format needs adjusting.

# LogIntel language support

The workspace uses Django's translation framework. English is the default language; Arabic is available with a right-to-left layout. The language selector applies the choice across workspace pages and remembers it for the session.

## Add another language

1. Add its Django language code and display name to `LANGUAGES` in `admin_LogIntel/settings.py`.
2. Create or update its catalog with `python manage.py makemessages -l <code>`.
3. Translate `locale/<code>/LC_MESSAGES/django.po`.
4. Compile catalogs with `python manage.py compilemessages`.
5. Add or update interface tests for the new language, especially direction and translated form labels.

New templates and Python interface strings should use Django's `{% trans %}` / `{% blocktrans %}` tags and `gettext_lazy` / `gettext`, respectively, so they remain available to translators.
