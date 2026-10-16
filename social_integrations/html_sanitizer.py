"""
Allow-list HTML sanitiser for rendering inbound email bodies on clients that
cannot run DOMPurify (the mobile app renders ``body_html_safe`` with
react-native-render-html).

Built on the stdlib ``html.parser`` because ``bleach`` is not a project
dependency. The rules are deliberately conservative:

- Only the tags in ``ALLOWED_TAGS`` survive; unknown tags are dropped but
  their text content is kept (``<font>``, ``<center>``, ``<html>``, ...).
- ``<script>``, ``<style>``, ``<head>``, ``<iframe>`` etc. are dropped
  *together with their content*.
- Only the attributes in ``ALLOWED_ATTRS`` survive; ``style``, ``class``,
  ``id`` and every ``on*`` handler are removed.
- ``href`` must be http(s) or mailto; ``img src`` must be http(s). Anything
  else (``javascript:``, ``data:``, ``cid:``) is dropped.
- Text is re-escaped, comments and doctype declarations are removed.
"""
from html import escape
from html.parser import HTMLParser

ALLOWED_TAGS = frozenset({
    'p', 'br', 'hr', 'a', 'b', 'i', 'u', 's', 'strong', 'em', 'small',
    'sub', 'sup', 'ul', 'ol', 'li', 'blockquote', 'pre', 'code',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'img',
    'table', 'thead', 'tbody', 'tfoot', 'tr', 'td', 'th',
    'span', 'div',
})

# Tags whose entire subtree is discarded.
DROP_CONTENT_TAGS = frozenset({
    'script', 'style', 'head', 'title', 'iframe', 'frame', 'frameset',
    'object', 'embed', 'applet', 'noscript', 'template', 'svg', 'math',
    'form', 'input', 'button', 'select', 'textarea',
})

VOID_TAGS = frozenset({'br', 'hr', 'img'})

ALLOWED_ATTRS = {
    'a': frozenset({'href', 'title'}),
    'img': frozenset({'src', 'alt', 'width', 'height'}),
    'td': frozenset({'colspan', 'rowspan'}),
    'th': frozenset({'colspan', 'rowspan'}),
}

HREF_SCHEMES = ('http://', 'https://', 'mailto:')
SRC_SCHEMES = ('http://', 'https://')


def _safe_url(value, schemes):
    if not value:
        return None
    candidate = value.strip().replace('\n', '').replace('\r', '').replace('\t', '')
    if candidate.lower().startswith(schemes):
        return candidate
    return None


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.open_tags = []
        self._drop_depth = 0
        self._drop_tag = None

    # -- helpers -----------------------------------------------------------

    def _clean_attrs(self, tag, attrs):
        allowed = ALLOWED_ATTRS.get(tag)
        if not allowed:
            return ''
        pieces = []
        for name, value in attrs:
            name = (name or '').lower()
            if name not in allowed:
                continue
            if name == 'href':
                value = _safe_url(value, HREF_SCHEMES)
            elif name == 'src':
                value = _safe_url(value, SRC_SCHEMES)
            if value is None:
                continue
            pieces.append(f' {name}="{escape(str(value), quote=True)}"')
        if tag == 'a' and any(p.startswith(' href=') for p in pieces):
            pieces.append(' target="_blank" rel="noopener noreferrer"')
        return ''.join(pieces)

    # -- parser callbacks --------------------------------------------------

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self._drop_depth:
            if tag == self._drop_tag and tag not in VOID_TAGS:
                self._drop_depth += 1
            return
        if tag in DROP_CONTENT_TAGS:
            self._drop_depth = 1
            self._drop_tag = tag
            return
        if tag not in ALLOWED_TAGS:
            return
        attr_text = self._clean_attrs(tag, attrs)
        if tag in VOID_TAGS:
            self.out.append(f'<{tag}{attr_text} />')
            return
        self.out.append(f'<{tag}{attr_text}>')
        self.open_tags.append(tag)

    def handle_startendtag(self, tag, attrs):
        tag = tag.lower()
        if tag in VOID_TAGS:
            self.handle_starttag(tag, attrs)
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._drop_depth:
            if tag == self._drop_tag:
                self._drop_depth -= 1
                if self._drop_depth == 0:
                    self._drop_tag = None
            return
        if tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        if tag not in self.open_tags:
            return
        # Close everything opened after this tag so the output stays balanced.
        while self.open_tags:
            open_tag = self.open_tags.pop()
            self.out.append(f'</{open_tag}>')
            if open_tag == tag:
                break

    def handle_data(self, data):
        if self._drop_depth:
            return
        self.out.append(escape(data, quote=False))

    def handle_entityref(self, name):  # pragma: no cover - convert_charrefs=True
        self.handle_data(f'&{name};')

    def handle_charref(self, name):  # pragma: no cover - convert_charrefs=True
        self.handle_data(f'&#{name};')

    def handle_comment(self, data):
        return

    def handle_decl(self, decl):
        return

    def handle_pi(self, data):
        return

    def unknown_decl(self, data):
        return

    def result(self):
        self.close()
        while self.open_tags:
            self.out.append(f'</{self.open_tags.pop()}>')
        return ''.join(self.out)


def sanitize_email_html(html):
    """Return ``html`` reduced to the allow-list above. Empty input → ''."""
    if not html:
        return ''
    parser = _Sanitizer()
    try:
        parser.feed(html)
        return parser.result()
    except Exception:  # pragma: no cover - malformed input must never 500
        return escape(html, quote=False)
