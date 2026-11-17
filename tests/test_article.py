import pytest

from mitblogs.parse import ParseError, is_not_found, parse_article

MODERN = 'https://mitadmissions.org/blogs/entry/i-have-seen-some-strange-things-around-mit-recently/'
UPLOADS = 'https://mitadmissions.org/wp-content/uploads/2026/11/'


@pytest.fixture
def modern(fixture):
    return parse_article(fixture('article_modern.html'), MODERN)


def test_modern_post_metadata(modern):
    assert modern.title == 'i have seen some strange things around mit recently'
    assert modern.subtitle == 'some hacks from fall 2026'
    assert modern.author == "Suzuko O. '30"
    assert modern.published == '2026-11-16T21:51:07-05:00'  # JSON-LD UTC time in Eastern Time
    assert modern.categories == ['Events', 'Hacks', 'How to MIT']


def test_images_include_gallery_slides_and_captioned_images(modern):
    assert len(modern.images) == 13
    assert modern.images[0] == UPLOADS + 'Screenshot-2026-11-16-at-6.53.39-PM-800x565.png'
    assert UPLOADS + 'pumpkin2-800x600.jpg' in modern.images  # lazy-loaded carousel slide
    assert modern.images[-1] == UPLOADS + 'ufo3-6-e1794874324927-800x936.jpeg'


def test_content_is_readable_text_with_footnotes(modern):
    content = modern.content

    assert content.startswith('a couple weeks ago, i looked out of my bedroom window')
    assert 'it’s the tetazoo squanch![1]\n\nlook at those balls' in content  # marker + gallery caption
    assert content.endswith(
        'Footnotes:\n[1] fun fact: every day, at 11:12, you can go to '
        'https://tetazoo.mit.edu/make-a-squanch to get your very own squanch! C:\n'
        '[2] mit’s student-run newspaper\n'
        '[3] the zesiger center, mit’s main gym that is on the other side of campus as my dorm\n'
        '[4] aka little dome\n'
        '[5] funny shaped building with two auditoria in it\n'
        '[6] the big lawn in front of the big dome'
    )
    assert content.count('fun fact') == 1  # the tooltip copy is not repeated inline
    for noise in ('back to text', 'Previous', 'Next', '\u2060', '\u200b', '\n\n\n'):
        assert noise not in content


def test_legacy_post_skips_the_hidden_archive_notice(fixture):
    article = parse_article(fixture('article_legacy.html'), 'https://mitadmissions.org/blogs/entry/welcome_2014s/')

    assert article.title == 'Welcome 2014s!! :)'
    assert article.author == "Chris S. '11"
    assert article.published == '2010-05-04T00:21:51-04:00'
    assert article.images == []
    assert 'archived historical post' not in article.content
    assert article.content.startswith('So we’re almost an hour past May 3')
    assert 'Being with you\nIs so dysfunctional\nI really shouldn’t miss you' in article.content


def test_oldest_post(fixture):
    article = parse_article(
        fixture('article_oldest.html'), 'https://mitadmissions.org/blogs/entry/theory_of_actual_reality/'
    )

    assert article.title == 'Theory of Actual Reality'
    assert article.subtitle == ''
    assert article.author == "Matt McGann '00"
    assert article.published == '2004-09-22T23:09:38-04:00'
    assert article.categories == ['Academics & Research', 'Admissions']
    assert article.content.startswith('I’m back in Boston now with five Central Meetings under my belt')


def test_cloudflare_protected_emails_are_decoded():
    key = 0x5A
    encoded = f'{key:02x}' + bytes(byte ^ key for byte in b'admissions@mit.edu').hex()
    page = (
        '<html><body><div class="article__body"><p>Write to <a href="/cdn-cgi/l/email-protection" '
        f'class="__cf_email__" data-cfemail="{encoded}">[email&#160;protected]</a>.</p></div></body></html>'
    )

    assert parse_article(page, MODERN).content == 'Write to admissions@mit.edu.'


def test_page_without_article_body_is_an_error():
    challenge = '<html><head><title>Just a moment...</title></head><body></body></html>'

    with pytest.raises(ParseError, match='Just a moment'):
        parse_article(challenge, MODERN)


@pytest.mark.parametrize('page', ['', '  \n'])
def test_empty_page_is_an_error_to_retry(page):
    assert not is_not_found(page)
    with pytest.raises(ParseError, match='unreadable page'):
        parse_article(page, MODERN)


def test_wordpress_404_page_is_detected(fixture):
    assert is_not_found('<html><body class="error404 page"><h1>Not found</h1></body></html>')
    assert not is_not_found(fixture('article_modern.html'))
