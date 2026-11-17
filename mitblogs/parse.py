"""Parsers for mitadmissions.org pages, shared by both crawlers.

The browsers only fetch and render pages; everything below works on HTML
strings, so it is unit-tested against saved pages in ``tests/fixtures``.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

from lxml import etree
from lxml import html as lxml_html
from lxml.html import HtmlElement

from .models import Article, Teaser
from .text import clean_inline, html_to_text, is_emoji_image

BLOGS_URL = 'https://mitadmissions.org/blogs/'
SITE_TZ = ZoneInfo('America/New_York')

# Lazy-loaded gallery slides keep the real URL in a data attribute.
IMAGE_SOURCES = ('data-flickity-lazyload-src', 'data-lazy-src', 'data-src', 'src')
DECORATIVE_IMAGE = re.compile(r's\.w\.org/images/core/emoji/|/images/smileys/|/wp-includes/images/smilies/')
DISPLAY_COUNT = re.compile(r'displayCount\((\{.*\})\)', re.DOTALL)


class ParseError(ValueError):
    """The page does not have the expected structure (error page, challenge, …)."""


def listing_url(page: int) -> str:
    return BLOGS_URL if page <= 1 else f'{BLOGS_URL}page/{page}/'


def parse_listing(page_html: str, base_url: str = BLOGS_URL) -> list[Teaser]:
    """Read the post cards of a listing page.

    Comment counts come in three flavours: static WordPress text ("44 comments"),
    an empty static text on posts whose comments were closed (0), and Disqus links
    whose text is filled in by ``count.js``. A Disqus link without a number yet
    gets ``comment_count=None`` and is resolved with :func:`apply_disqus_counts`.
    """
    doc = _document(page_html)
    teasers = []
    for card in doc.cssselect('article.tease--post'):
        link = _first(card, 'a.post-tease__h__link')
        if link is None or not link.get('href'):
            continue
        author = _text(_first(card, '.post-tease__meta-item--author a')) or _strip_by(
            _text(_first(card, '.post-tease__meta-item--author'))
        )
        comments = _first(card, '.tease__meta-item--comments')
        disqus = _first(comments, 'a[data-disqus-identifier]') if comments is not None else None
        count_text = _text(comments)
        if disqus is not None:
            count = _parse_count(count_text) if re.search(r'\d', count_text) else None
        else:
            count = _parse_count(count_text) if comments is not None else None
        teasers.append(
            Teaser(
                url=urljoin(base_url, link.get('href')),
                title=_text(_first(card, '.post-tease__title')),
                subtitle=_text(_first(card, '.post-tease__subtitle')),
                author=author,
                date=_iso_date(_text(_first(card, '.tease__meta-item--date'))),
                comment_count=count,
                disqus_id=disqus.get('data-disqus-identifier') if disqus is not None else None,
            )
        )
    return teasers


def parse_last_page(page_html: str) -> int:
    """Highest page number in the listing pagination (1 if there is none)."""
    numbers = [_text(el).replace(',', '') for el in _document(page_html).cssselect('.page-numbers')]
    return max((int(n) for n in numbers if n.isdigit()), default=1)


def parse_disqus_counts(script: str | None) -> dict[str, int] | None:
    """Parse a ``count-data.js`` JSONP response into ``{thread id: comments}``.

    Returns None if the response is not a count payload (error page, empty body).
    """
    match = DISPLAY_COUNT.search(script or '')
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None
    return {item['id']: int(item['comments']) for item in data.get('counts', []) if 'id' in item}


def disqus_request_ids(url: str) -> list[str]:
    """Thread ids asked for in a ``count-data.js?1=<id>&1=<id>`` request."""
    return parse_qs(urlsplit(url).query).get('1', [])


def apply_disqus_counts(teasers: Iterable[Teaser], counts: dict[str, int], requested: set[str]) -> list[Teaser]:
    """Fill in Disqus comment counts and return the teasers that are still unknown.

    Disqus leaves threads without comments out of its answer, so an id that was
    requested but is missing from the counts has 0 comments.
    """
    unknown = []
    for teaser in teasers:
        if teaser.disqus_id is None:
            continue
        if teaser.disqus_id in counts:
            teaser.comment_count = counts[teaser.disqus_id]
        elif teaser.disqus_id in requested:
            teaser.comment_count = 0
        elif teaser.comment_count is None:
            unknown.append(teaser)
    return unknown


def parse_article(page_html: str, url: str) -> Article:
    doc = _document(page_html)
    decode_cloudflare_emails(doc)
    body = _first(doc, 'div.article__body')
    if body is None:
        raise ParseError(f'no article body in {url} (title: {_text(_first(doc, "title"))!r})')

    heading = _first(doc, 'h1.page-topper__title')
    content = html_to_text(body)
    footnotes = _footnotes(doc, body)
    if footnotes:
        content += '\n\nFootnotes:\n' + '\n'.join(f'[{label}] {text}' for label, text in footnotes)

    return Article(
        url=url,
        title=_heading_title(heading),
        subtitle=_text(_first(doc, '.page-topper__sub')),
        author=_strip_by(_text(_first(doc, '.page-topper__title__name'))),
        published=_published(doc),
        categories=[_text(a) for a in doc.cssselect('.page-topper__category-link')],
        content=content,
        images=_images(body, url),
    )


def decode_cloudflare_emails(root: HtmlElement) -> None:
    """Cloudflare serves e-mail addresses as "[email protected]" and decodes them
    with a script; decode them here so the text does not depend on JavaScript."""
    for el in root.cssselect('[data-cfemail]'):
        email = _decode_cfemail(el.get('data-cfemail', ''))
        if email is not None:
            for child in list(el):
                el.remove(child)
            el.text = email


def _decode_cfemail(encoded: str) -> str | None:
    """The first byte is an XOR key for the UTF-8 bytes that follow."""
    try:
        data = bytes.fromhex(encoded)
    except ValueError:
        return None
    if len(data) < 2:
        return None
    return bytes(byte ^ data[0] for byte in data[1:]).decode('utf-8', errors='replace')


def is_not_found(page_html: str) -> bool:
    try:
        body = _first(_document(page_html), 'body')
    except ParseError:
        return False
    return body is not None and 'error404' in (body.get('class') or '').split()


def _document(page_html: str) -> HtmlElement:
    try:
        return lxml_html.document_fromstring(page_html)
    except etree.ParserError as exc:  # an empty page, read while the browser was replacing it
        raise ParseError(f'unreadable page: {exc}') from exc


def _first(el: HtmlElement | None, selector: str) -> HtmlElement | None:
    if el is None:
        return None
    found = el.cssselect(selector)
    return found[0] if found else None


def _text(el: HtmlElement | None) -> str:
    return clean_inline(el.text_content()) if el is not None else ''


def _strip_by(text: str) -> str:
    return re.sub(r'^by\s+', '', text, flags=re.IGNORECASE)


def _parse_count(text: str) -> int:
    match = re.search(r'\d[\d,]*', text)
    return int(match.group().replace(',', '')) if match else 0


def _iso_date(text: str) -> str:
    try:
        return datetime.strptime(text, '%B %d, %Y').date().isoformat()
    except ValueError:
        return text


def _heading_title(heading: HtmlElement | None) -> str:
    """The <h1> holds "<title> <a><span>by Author</span></a>"; keep the title only."""
    if heading is None:
        return ''
    heading = copy.deepcopy(heading)
    for name in heading.cssselect('.page-topper__title__name'):
        parent = name.getparent()
        (parent if parent is not None and parent.tag == 'a' else name).drop_tree()
    return _text(heading)


def _published(doc: HtmlElement) -> str:
    """Exact publication time from JSON-LD, falling back to the visible date."""
    for script in doc.cssselect('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text_content())
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get('@graph', [data])
        for item in items:
            if isinstance(item, dict) and item.get('datePublished'):
                return _site_time(item['datePublished'])
    time_el = _first(doc, 'time.page-topper__date')
    return time_el.get('datetime', '') if time_el is not None else ''


def _site_time(value: str) -> str:
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return value
    if moment.tzinfo is not None:
        moment = moment.astimezone(SITE_TZ)
    return moment.isoformat()


def _footnotes(doc: HtmlElement, body: HtmlElement) -> list[tuple[str, str]]:
    """Footnotes listed under the post, or the inline tooltips if there is no list."""
    notes = []
    for index, item in enumerate(doc.cssselect('ol.page__footnotes > li.page__footnote'), start=1):
        match = re.search(r'(\d+)$', item.get('id') or '')
        notes.append((match.group(1) if match else str(index), html_to_text(_as_block(item))))
    if notes:
        return notes
    tooltips = body.cssselect('.annotation')
    return [(str(index), html_to_text(_as_block(el))) for index, el in enumerate(tooltips, start=1)]


def _as_block(el: HtmlElement) -> HtmlElement:
    """Detached copy rendered as a plain block: no list bullet, no hiding class."""
    el = copy.deepcopy(el)
    el.tag = 'div'
    for attr in ('class', 'hidden', 'style'):
        el.attrib.pop(attr, None)
    return el


def _images(body: HtmlElement, base_url: str) -> list[str]:
    images: list[str] = []
    for img in body.iter('img'):
        if is_emoji_image(img) or any(True for _ in img.iterancestors('noscript')):
            continue
        source = next((value.strip() for attr in IMAGE_SOURCES if (value := img.get(attr)) and value.strip()), '')
        if not source or source.startswith('data:'):
            continue
        image_url = urljoin(base_url, source)
        if not DECORATIVE_IMAGE.search(image_url) and image_url not in images:
            images.append(image_url)
    return images
