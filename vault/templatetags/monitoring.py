"""
Template helpers for the workspace interface.

Keeping these here -- instead of repeating markup -- is what lets the filter
fields, the icons and the progress readout share one definition across the
search, records, references and import screens.
"""

from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

_ICON_PATHS = {
    'search': '<circle cx="7" cy="7" r="4.2"/><path d="M10.1 10.1 14 14"/>',
    'globe': ('<circle cx="8" cy="8" r="6"/>'
              '<path d="M2 8h12"/><ellipse cx="8" cy="8" rx="2.9" ry="6"/>'),
    'link': ('<path d="M6.7 9.3a2.8 2.8 0 0 0 4 0l1.9-1.9a2.8 2.8 0 1 0-4-4'
             'l-.9.9"/><path d="M9.3 6.7a2.8 2.8 0 0 0-4 0L3.4 8.6a2.8 2.8 '
             '0 1 0 4 4l.9-.9"/>'),
    'mail': ('<rect x="1.8" y="3.2" width="12.4" height="9.6" rx="1.6"/>'
             '<path d="m2.6 4.6 5.4 3.9 5.4-3.9"/>'),
    'user': '<circle cx="8" cy="5.6" r="2.6"/><path d="M2.9 13.4a5.1 5.1 0 0 1 10.2 0"/>',
    'users': ('<circle cx="6" cy="6" r="2.3"/>'
              '<path d="M1.6 13.2a4.6 4.6 0 0 1 8.8 0"/>'
              '<path d="M10.6 3.9a2.3 2.3 0 0 1 0 4.3M11.6 9.2a4.6 4.6 0 0 1 2.8 4"/'),
    'key': ('<circle cx="5.2" cy="10.8" r="2.5"/>'
            '<path d="M7 9 12.9 3.1M11.1 4.9 12.8 6.6M9.4 6.6l1.4 1.4"/>'),
    'hash': '<path d="M6.2 2.4 4.7 13.6M11.3 2.4 9.8 13.6M2.7 5.8h11M2.5 10.2h11"/>',
    'calendar': ('<rect x="2" y="3.2" width="12" height="10.6" rx="1.6"/>'
                 '<path d="M2 6.4h12M5.2 1.9v2.5M10.8 1.9v2.5"/>'),
    'clock': '<circle cx="8" cy="8" r="6"/><path d="M8 4.7V8l2.4 1.6"/>',
    'x': '<path d="m4.2 4.2 7.6 7.6M11.8 4.2l-7.6 7.6"/>',
    'check': '<path d="m3.2 8.4 3.2 3.2 6.4-7"/>',
    'plus': '<path d="M8 3.2v9.6M3.2 8h9.6"/>',
    'upload': ('<path d="M8 10.4V2.6M4.9 5.7 8 2.6l3.1 3.1"/>'
               '<path d="M2.4 10.9v1.7a1 1 0 0 0 1 1h9.2a1 1 0 0 0 1-1v-1.7"/>'),
    'download': ('<path d="M8 2.6v7.8M4.9 6.7 8 9.8l3.1-3.1"/>'
                 '<path d="M2.4 10.9v1.7a1 1 0 0 0 1 1h9.2a1 1 0 0 0 1-1v-1.7"/>'),
    'folder': ('<path d="M1.8 12.6V4.4a1 1 0 0 1 1-1h3.3l1.4 1.8h5.7a1 1 0 0 1 '
               '1 1v6.4a1 1 0 0 1-1 1H2.8a1 1 0 0 1-1-1Z"/>'),
    'file': ('<path d="M9.4 1.9H3.9a1 1 0 0 0-1 1v10.2a1 1 0 0 0 1 1h8.2a1 1 0 '
             '0 0 1-1V5.5z"/><path d="M9.4 1.9v3.6H13"/>'),
    'files': ('<path d="M5.4 4.6h6.7a1 1 0 0 1 1 1v7.8a1 1 0 0 1-1 1H5.4a1 1 0 '
              '0 1-1-1V5.6a1 1 0 0 1 1-1Z"/>'
              '<path d="M10.6 2.2H4.2a1 1 0 0 0-1 1v8"/>'),
    'layers': ('<path d="M8 1.8 1.9 4.9 8 8l6.1-3.1z"/>'
               '<path d="M1.9 8 8 11.2 14.1 8"/>'
               '<path d="M1.9 11.2 8 14.4l6.1-3.2"/>'),
    'database': ('<ellipse cx="8" cy="3.9" rx="5.3" ry="2.1"/>'
                 '<path d="M2.7 3.9v8.2c0 1.2 2.4 2.1 5.3 2.1s5.3-.9 5.3-2.1V3.9"/>'
                 '<path d="M2.7 8c0 1.2 2.4 2.1 5.3 2.1S13.3 9.2 13.3 8"/>'),
    'sliders': ('<path d="M2.4 4.7h6.9M12.6 4.7h1M2.4 11.3h1M6.9 11.3h6.7"/>'
                '<circle cx="11.1" cy="4.7" r="1.5"/><circle cx="5" cy="11.3" r="1.5"/>'),
    'funnel': '<path d="M2.4 3.2h11.2L9.3 8.2v4.3l-2.6 1.3V8.2z"/>',
    'chevron-down': '<path d="m4.2 6.2 3.8 3.8 3.8-3.8"/>',
    'alert': ('<path d="M8 2.6 1.9 13.1h12.2z"/><path d="M8 6.4v3"/>'
              '<path d="M8 11.2h.01"/>'),
    'info': '<circle cx="8" cy="8" r="6"/><path d="M8 7.3v3.9M8 5.2h.01"/>',
    'arrow-right': '<path d="M2.8 8h10.2M9.5 4.5 13 8l-3.5 3.5"/>',
    'trash': '<path d="M2.9 4.4h10.2M6.1 2.6h3.8M4.2 4.4l.6 9h6.4l.6-9"/>',
    'pencil': '<path d="M11.1 2.5 13.5 4.9 5.3 13.1H2.7v-2.6z"/>',
    'eye': ('<path d="M1.6 8S3.9 3.7 8 3.7 14.4 8 14.4 8 12.1 12.3 8 12.3 '
            '1.6 8 1.6 8Z"/><circle cx="8" cy="8" r="1.9"/>'),
    'refresh': '<path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9"/><path d="M13.9 2v2.7h-2.7"/>',
    'activity': '<path d="M1.8 8.6h3L6.1 4l2.6 8 1.5-3.4h3.9"/>',
    'shield': ('<path d="M8 1.8 3 3.9v4.2c0 3 2.1 5.1 5 5.9 2.9-.8 5-2.9 5-5.9'
               'V3.9z"/>'),
    'stop': '<rect x="4.1" y="4.1" width="7.8" height="7.8" rx="1.4"/>',
    'sort': ('<path d="M4.4 2.9v10.2M4.4 13.1 2.4 11.1M4.4 13.1l2-2M11.6 13.1'
             'V2.9M11.6 2.9l-2 2M11.6 2.9l2 2"/>'),
    'bolt': '<path d="M9.1 1.9 3.6 9h3.6l-.4 5.1L12.4 7H8.7z"/>',
    'terminal': ('<rect x="1.8" y="2.8" width="12.4" height="10.4" rx="1.6"/>'
                 '<path d="M4.6 6.4 6.8 8.6 4.6 10.8M9 11h2.8"/>'),
}


@register.simple_tag
def icon(name, css_class='', aria_label=''):
    """
    An <svg> that references the page sprite.

    Icons are drawn from one shared set of symbols, so they inherit
    ``currentColor`` (and therefore the theme) and cost no extra request.
    """
    if name not in _ICON_PATHS:
        return mark_safe('')
    classes = ('icon ' + css_class).strip()
    if aria_label:
        return format_html(
            '<svg class="{}" viewBox="0 0 16 16" role="img" aria-label="{}">'
            '<use href="#i-{}"></use></svg>',
            classes, aria_label, name,
        )
    return format_html(
        '<svg class="{}" viewBox="0 0 16 16" aria-hidden="true">'
        '<use href="#i-{}"></use></svg>',
        classes, name,
    )


@register.simple_tag
def icon_sprite():
    """
    The sprite itself, included once per page.

    Icons are referenced with ``<use>``, so they inherit currentColor and stay
    crisp in both themes without shipping an icon font.
    """
    symbols = ''.join(
        f'<symbol id="i-{name}" viewBox="0 0 16 16">{paths}</symbol>'
        for name, paths in _ICON_PATHS.items()
    )
    return format_html(
        '<svg xmlns="http://www.w3.org/2000/svg" class="icon-sprite" '
        'aria-hidden="true" focusable="false">{}</svg>',
        mark_safe(symbols),
    )


@register.filter
def bytes_size(value):
    """Human readable byte count, e.g. ``1.4 MB``."""
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return value
    for unit in ('B', 'KB', 'MB', 'GB', 'TB', 'PB'):
        if size < 1024 or unit == 'PB':
            return f'{int(size)} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{size:.1f} PB'


@register.filter
def duration(value):
    """Short elapsed time, e.g. ``4s``, ``2m 15s``, ``1h 04m``."""
    try:
        seconds = int(float(value or 0))
    except (TypeError, ValueError):
        return value
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        minutes, rest = divmod(seconds, 60)
        return f'{minutes}m {rest:02d}s'
    hours, rest = divmod(seconds, 3600)
    return f'{hours}h {rest // 60:02d}m'


@register.simple_tag
def query_without(params, *drop):
    """
    Query string with *drop* removed -- the link target of a "clear filter"
    chip, kept in one place so every filter row behaves the same.
    """
    try:
        mutable = params.copy()
    except AttributeError:
        return ''
    for key in list(drop) + ['page']:
        mutable.pop(key, None)
    encoded = mutable.urlencode()
    return f'?{encoded}' if encoded else '?'
