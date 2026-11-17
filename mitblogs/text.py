"""Plain-text rendering of article HTML.

`lxml`'s ``text_content()`` glues paragraphs together and keeps hidden helper
text (tooltips, "back to text" links, carousel buttons), so article bodies are
rendered with a small block-aware walker instead.
"""

from __future__ import annotations

import re

from lxml.html import HtmlElement

BLOCK_TAGS = frozenset(
    'address article aside blockquote center dd details div dl dt fieldset figcaption figure footer form '
    'h1 h2 h3 h4 h5 h6 header hr main nav ol p pre section summary table ul'.split()
)
LINE_TAGS = frozenset('caption li tr'.split())
SKIP_TAGS = frozenset(
    'audio button canvas embed iframe input map noscript object picture script select source style svg '
    'template textarea video'.split()
)
# "annotation" is the tooltip copy of a footnote; footnotes are appended once at the end.
SKIP_CLASSES = frozenset(
    'annotation carousel-nav page__footnote__link page__footnotes screen-reader-text visually-hidden'.split()
)
EMOJI_CLASSES = frozenset({'emoji', 'wp-smiley'})

# Zero-width space, word joiner, BOM and soft hyphen. ZWJ/ZWNJ are kept because
# emoji sequences and some scripts depend on them.
_INVISIBLE = dict.fromkeys(map(ord, '\u200b\u2060\ufeff\u00ad'))
_WHITESPACE = re.compile(r'\s+')
_BLANK_LINES = re.compile(r'\n{3,}')
_DISPLAY_NONE = re.compile(r'display\s*:\s*none', re.IGNORECASE)


def clean_inline(text: str | None) -> str:
    """Collapse whitespace and drop invisible characters in a short text."""
    return _WHITESPACE.sub(' ', (text or '').translate(_INVISIBLE)).strip()


def is_emoji_image(img: HtmlElement) -> bool:
    return bool(EMOJI_CLASSES & _classes(img))


def html_to_text(root: HtmlElement) -> str:
    """Render an element as plain text: one blank line between blocks,
    single newlines for ``<br>`` and list items, footnote markers as ``[n]``."""
    tokens: list[tuple[str, str]] = []
    _collect(root, tokens, in_pre=False)
    return _render(tokens)


def _classes(el: HtmlElement) -> set[str]:
    return set((el.get('class') or '').split())


def _collect(el: HtmlElement, tokens: list[tuple[str, str]], in_pre: bool) -> None:
    if not isinstance(el.tag, str):  # comments and processing instructions
        return
    tag = el.tag.lower()
    classes = _classes(el)

    if tag == 'br':
        tokens.append(('br', ''))
        return
    if tag == 'img':
        if classes & EMOJI_CLASSES and el.get('alt'):
            tokens.append(('text', el.get('alt')))
        return
    if tag in SKIP_TAGS or classes & SKIP_CLASSES or _is_hidden(el):
        return
    if 'annotation__number' in classes:
        tokens.append(('text', f'[{_footnote_label(el)}]'))
        return

    in_pre = in_pre or tag == 'pre'
    text_kind = 'pre' if in_pre else 'text'
    boundary = 'para' if tag in BLOCK_TAGS else 'line' if tag in LINE_TAGS else None

    if boundary:
        tokens.append((boundary, ''))
    if tag == 'li':
        tokens.append(('text', _bullet(el)))
    if el.text:
        tokens.append((text_kind, el.text))
    for child in el:
        _collect(child, tokens, in_pre)
        if child.tail:
            tokens.append((text_kind, child.tail))
    if tag in ('td', 'th'):
        tokens.append(('text', ' '))
    if boundary:
        tokens.append((boundary, ''))


def _is_hidden(el: HtmlElement) -> bool:
    """Not rendered by the browser, e.g. the archive notice on old posts."""
    return el.get('hidden') is not None or bool(_DISPLAY_NONE.search(el.get('style') or ''))


def _footnote_label(el: HtmlElement) -> str:
    label = clean_inline(el.text_content())
    return str(int(label)) if label.isdigit() else label


def _bullet(li: HtmlElement) -> str:
    parent = li.getparent()
    if parent is None or parent.tag != 'ol':
        return '- '
    start = parent.get('start', '1')
    first = int(start) if start.lstrip('-').isdigit() else 1
    items = [child for child in parent if child.tag == 'li']
    return f'{first + items.index(li)}. '


def _render(tokens: list[tuple[str, str]]) -> str:
    lines: list[str] = []
    parts: list[str] = []
    raw = False  # the current line contains <pre> text, so keep its spacing

    def end_line() -> str:
        nonlocal raw
        line = ''.join(parts)
        parts.clear()
        line = line.rstrip() if raw else _WHITESPACE.sub(' ', line).strip()
        raw = False
        return line

    for kind, value in tokens:
        if kind == 'text':
            parts.append(_WHITESPACE.sub(' ', value.translate(_INVISIBLE)))
        elif kind == 'pre':
            for index, chunk in enumerate(value.translate(_INVISIBLE).split('\n')):
                if index:
                    lines.append(end_line())
                parts.append(chunk)
                raw = True
        elif kind == 'br':
            lines.append(end_line())  # may be empty: <br><br> separates paragraphs
        else:
            line = end_line()
            if line:
                lines.append(line)
            if kind == 'para':
                lines.append('')
    lines.append(end_line())

    return _BLANK_LINES.sub('\n\n', '\n'.join(lines)).strip()
