from mitblogs.models import Teaser
from mitblogs.parse import (
    apply_disqus_counts,
    disqus_request_ids,
    parse_disqus_counts,
    parse_last_page,
    parse_listing,
)


def test_reads_post_cards_of_a_disqus_era_page(fixture):
    teasers = parse_listing(fixture('listing_disqus.html'))

    assert len(teasers) == 16
    assert teasers[0] == Teaser(
        url='https://mitadmissions.org/blogs/entry/i-have-never-been-this-unprepared-for-a-test/',
        title='i have never been this unprepared for a test',
        subtitle='what a rude awakening',
        author="Suzuko O. '30",
        date='2026-10-28',
        comment_count=None,
        disqus_id='129688 https://mitadmissions.org/?p=129688',
    )
    assert all(t.disqus_id and t.comment_count is None for t in teasers)
    assert teasers[2].author == "Anika H. '26, MEng '27"


def test_reads_static_wordpress_comment_counts(fixture):
    teasers = parse_listing(fixture('listing_legacy.html'))

    assert [t.comment_count for t in teasers] == [44, 9, 8, 6, 3, 4, 7, 2, 15, 20, 6, 16, 7, 13, 56, 34]
    assert all(t.disqus_id is None for t in teasers)
    assert teasers[0].title == 'Computers and their Programs'
    assert teasers[0].date == '2012-02-15'


def test_oldest_posts_with_closed_comments_count_zero(fixture):
    html = fixture('listing_last.html')
    teasers = parse_listing(html)

    assert len(teasers) == 8
    assert {t.comment_count for t in teasers} == {0}
    assert teasers[0].subtitle == ''
    assert parse_last_page(html) == 492


def test_pagination_gives_the_last_page(fixture):
    assert parse_last_page(fixture('listing_disqus.html')) == 492
    assert parse_last_page('<html><body><p>no pagination</p></body></html>') == 1


def test_resolves_disqus_counts_from_both_batches(fixture):
    teasers = parse_listing(fixture('listing_disqus.html'))
    counts, requested = {}, set()
    for batch in (0, 1):
        counts |= parse_disqus_counts(fixture(f'count_data_{batch}.js'))
        requested |= set(disqus_request_ids(fixture(f'count_data_{batch}.url').strip()))

    unknown = apply_disqus_counts(teasers, counts, requested)

    assert unknown == []
    assert len(requested) == 16
    assert [t.comment_count for t in teasers] == [9, 1, 21, 13, 1, 5, 3, 2, 1, 5, 7, 3, 5, 5, 5, 4]


def test_requested_thread_missing_from_the_answer_has_no_comments():
    teaser = Teaser('u', 't', '', 'a', '2020-01-01', disqus_id='42 https://mitadmissions.org/?p=42')

    unknown = apply_disqus_counts([teaser], counts={}, requested={teaser.disqus_id})

    assert unknown == []
    assert teaser.comment_count == 0


def test_thread_that_was_never_requested_stays_unknown():
    teaser = Teaser('u', 't', '', 'a', '2020-01-01', disqus_id='mitadmissions_6291')

    assert apply_disqus_counts([teaser], counts={}, requested=set()) == [teaser]
    assert teaser.comment_count is None


def test_disqus_payload_parsing():
    payload = 'DISQUSWIDGETS.displayCount({"text":{},"counts":[{"id":"7 https:\\/\\/x\\/?p=7","comments":3}]});'

    assert parse_disqus_counts(payload) == {'7 https://x/?p=7': 3}
    assert parse_disqus_counts('DISQUSWIDGETS.displayCount({"counts":[]});') == {}
    assert parse_disqus_counts('<html>Bad gateway</html>') is None
    assert parse_disqus_counts(None) is None


def test_request_ids_are_url_decoded():
    url = 'https://mitadmissions.disqus.com/count-data.js?1=130413%20https%3A%2F%2Fmitadmissions.org%2F%3Fp%3D130413&1=mitadmissions_6291'

    assert disqus_request_ids(url) == ['130413 https://mitadmissions.org/?p=130413', 'mitadmissions_6291']
