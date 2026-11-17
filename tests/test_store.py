from mitblogs.models import CSV_COLUMNS, REQUIRED_COLUMNS, BlogPost
from mitblogs.store import Checkpoint, read_csv, write_csv


def make_post(url: str, page: int = 1, position: int = 0, **changes) -> BlogPost:
    fields = dict(
        url=url,
        title='Title, with "quotes"',
        subtitle='',
        author="Someone '27",
        time='2026-11-16T21:51:07-05:00',
        comment_count=3,
        categories=['Events', 'Hacks'],
        content='First paragraph.\n\nSecond paragraph.',
        images=['https://example.org/a.png', 'https://example.org/b.jpg'],
        page=page,
        position=position,
    )
    return BlogPost(**(fields | changes))


def test_csv_has_the_required_columns_first(tmp_path):
    path = tmp_path / 'posts.csv'
    write_csv([make_post('https://x/1')], path)

    assert (
        CSV_COLUMNS[:6]
        == REQUIRED_COLUMNS
        == (
            'Title',
            'Author',
            'Comment Count',
            'Time',
            'Article Content',
            'Images In Article',
        )
    )
    header = b'Title,Author,Comment Count,Time,Article Content,Images In Article,URL,Subtitle,Categories\r\n'
    assert path.read_bytes().startswith(b'\xef\xbb\xbf' + header)  # BOM so Excel reads UTF-8


def test_csv_round_trip_keeps_multiline_cells_and_listing_order(tmp_path):
    path = tmp_path / 'posts.csv'
    posts = [
        make_post('https://x/3', page=2, position=0),
        make_post('https://x/1', page=1, position=0, comment_count=None),
        make_post('https://x/2', page=1, position=1, content='x' * 200_000),
    ]

    assert write_csv(posts, path) == 3
    rows = read_csv(path)

    assert [row['URL'] for row in rows] == ['https://x/1', 'https://x/2', 'https://x/3']
    assert rows[0]['Title'] == 'Title, with "quotes"'
    assert rows[0]['Comment Count'] == ''  # unknown, not zero
    assert rows[2]['Comment Count'] == '3'
    assert rows[0]['Article Content'] == 'First paragraph.\n\nSecond paragraph.'
    assert rows[1]['Article Content'] == 'x' * 200_000
    assert rows[0]['Images In Article'].split('\n') == ['https://example.org/a.png', 'https://example.org/b.jpg']
    assert rows[0]['Categories'] == 'Events; Hacks'


def test_checkpoint_resumes_and_skips_damaged_lines(tmp_path):
    path = tmp_path / 'posts.jsonl'
    checkpoint = Checkpoint(path, resume=True)
    checkpoint.add(make_post('https://x/1'))
    checkpoint.add(make_post('https://x/2'))
    checkpoint.close()
    with path.open('a', encoding='utf-8') as file:
        file.write('{"url": "https://x/3", "title": "cut off by a cra')

    resumed = Checkpoint(path, resume=True)
    assert 'https://x/1' in resumed and 'https://x/2' in resumed
    assert len(resumed) == 2
    assert resumed.posts['https://x/1'] == make_post('https://x/1')
    resumed.close()

    restarted = Checkpoint(path, resume=False)
    assert len(restarted) == 0
    restarted.close()
    assert path.read_text(encoding='utf-8') == ''
