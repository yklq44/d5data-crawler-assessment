from pathlib import Path

import pytest

from mitblogs.crawl import CrawlRun, desktop_user_agent, is_wanted_request, parse_options, read_listing
from mitblogs.models import Article, Teaser
from mitblogs.parse import ParseError


@pytest.mark.parametrize(
    ('value', 'expected'),
    [('all', (1, None)), ('5', (1, 5)), ('3-10', (3, 10)), ('7-', (7, None)), ('4-4', (4, 4))],
)
def test_page_ranges(value, expected):
    options = parse_options('Playwright', ['--pages', value])
    assert (options.first_page, options.last_page) == expected


@pytest.mark.parametrize('value', ['0', '10-3', 'abc', '-5'])
def test_invalid_page_ranges_are_rejected(value):
    with pytest.raises(SystemExit):
        parse_options('Playwright', [f'--pages={value}'])


def test_defaults_and_stop_page():
    options = parse_options('DrissionPage', [])
    assert options.output == Path('output/mit_blogs_drissionpage.csv')
    assert options.checkpoint == Path('output/mit_blogs_drissionpage.jsonl')
    assert options.stop_page(492) == 492
    assert parse_options('DrissionPage', ['--pages', '3']).stop_page(492) == 3
    assert parse_options('DrissionPage', ['--pages', '400-600']).stop_page(492) == 492


def test_headless_marker_is_removed_from_the_user_agent():
    headless = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/143.0.0.0'
    assert desktop_user_agent(headless).endswith('(KHTML, like Gecko) Chrome/143.0.0.0')


@pytest.mark.parametrize(
    ('url', 'resource_type', 'main_frame', 'expected'),
    [
        ('https://mitadmissions.org/blogs/page/2/', 'document', True, True),
        ('https://mitadmissions.org/blogs/entry/x/', 'Document', True, True),  # CDP spelling
        ('https://www.youtube.com/embed/abc', 'document', False, False),  # iframe
        ('https://mitadmissions.disqus.com/count.js', 'script', True, True),
        ('https://mitadmissions.disqus.com/count-data.js?1=6291', 'Script', True, True),
        ('https://mitadmissions.disqus.com/embed.js', 'script', True, False),
        ('https://html5.kaltura.org/js', 'Script', True, False),
        ('https://mitadmissions.org/wp-content/uploads/a.jpg', 'image', True, False),
    ],
)
def test_only_the_page_and_disqus_counts_are_loaded(url, resource_type, main_frame, expected):
    assert is_wanted_request(url, resource_type, main_frame) is expected


def test_read_listing_rejects_pages_without_posts(fixture):
    listing = read_listing(fixture('listing_legacy.html'), 260)
    assert (listing.number, len(listing.teasers), listing.last_page) == (260, 16, 492)
    with pytest.raises(ParseError):
        read_listing('<html><body>maintenance</body></html>', 5)


def test_incomplete_posts_are_written_but_not_checkpointed(tmp_path):
    options = parse_options('Playwright', ['--output', str(tmp_path / 'out.csv')])
    run = CrawlRun(options)
    done = Teaser('https://x/1', 'Done', '', 'A', '2020-01-01', comment_count=2)
    failed = Teaser('https://x/2', 'Failed', '', 'B', '2020-01-02', comment_count=0)
    no_count = Teaser('https://x/3', 'No count', '', 'C', '2020-01-03', disqus_id='3')
    article = Article(url='', title='Done', subtitle='', author='A', published='2020-01-01T10:00:00-05:00')

    run.record(done, article, page=1, position=0)
    run.record(failed, None, page=1, position=1)
    run.record(no_count, article, page=1, position=2)

    assert [t.url for _, t in run.todo([done, failed, no_count])] == ['https://x/2', 'https://x/3']
    assert run.finish() == 1  # incomplete posts: exit status asks for a rerun
    assert (tmp_path / 'out.csv').exists()
    assert len((tmp_path / 'out.jsonl').read_text(encoding='utf-8').splitlines()) == 1
