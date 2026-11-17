"""Command line, rate limiting and bookkeeping shared by both crawlers."""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import re
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .models import Article, BlogPost, Teaser
from .parse import ParseError, listing_url, parse_last_page, parse_listing
from .store import Checkpoint, write_csv

log = logging.getLogger('mitblogs')

DISQUS_WAIT = 15.0  # seconds to wait for the comment counts of a listing page
DISQUS_COUNTS = 'disqus.com/count'  # count.js and the count-data.js requests it makes


class CrawlError(Exception):
    """A page could not be loaded and parsed after all retries."""


class PageGone(CrawlError):
    """The page returned 404, e.g. a post deleted after the listing was read."""


@dataclass(slots=True)
class Listing:
    number: int
    teasers: list[Teaser]
    last_page: int


def read_listing(page_html: str, number: int) -> Listing:
    teasers = parse_listing(page_html, listing_url(number))
    if not teasers:
        raise ParseError(f'no posts on listing page {number}')
    return Listing(number, teasers, parse_last_page(page_html))


@dataclass(slots=True)
class Options:
    first_page: int
    last_page: int | None  # None: up to the last page of the listing
    concurrency: int
    delay: float
    timeout: float
    retries: int
    output: Path
    restart: bool
    headed: bool
    verbose: bool

    @property
    def checkpoint(self) -> Path:
        return self.output.with_suffix('.jsonl')

    def stop_page(self, site_last_page: int) -> int:
        return site_last_page if self.last_page is None else min(self.last_page, site_last_page)


def parse_options(tool: str, argv: list[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(
        description=f'Crawl https://mitadmissions.org/blogs/ with {tool} and save every post to a CSV file.',
    )
    parser.add_argument(
        '--pages',
        type=_page_range,
        default=(1, None),
        metavar='RANGE',
        help='listing pages to crawl: "all" (default), "N" for the first N pages, or "A-B"',
    )
    parser.add_argument('--concurrency', type=_positive_int, default=4, help='posts loaded in parallel (default: 4)')
    parser.add_argument(
        '--delay',
        type=float,
        default=0.25,
        help='minimum seconds between two page loads, across all tabs (default: 0.25)',
    )
    parser.add_argument('--timeout', type=float, default=45, help='page load timeout in seconds (default: 45)')
    parser.add_argument('--retries', type=_positive_int, default=3, help='attempts per page (default: 3)')
    default_output = Path('output') / f'mit_blogs_{tool.lower()}.csv'
    parser.add_argument(
        '--output',
        type=Path,
        default=default_output,
        help=f'CSV file to write (default: {default_output.as_posix()}); a .jsonl checkpoint is kept next to it',
    )
    parser.add_argument('--restart', action='store_true', help='ignore the checkpoint of a previous run')
    parser.add_argument('--headed', action='store_true', help='show the browser window')
    parser.add_argument('-v', '--verbose', action='store_true', help='log every page')
    args = parser.parse_args(argv)
    first, last = args.pages
    return Options(
        first_page=first,
        last_page=last,
        concurrency=args.concurrency,
        delay=args.delay,
        timeout=args.timeout,
        retries=args.retries,
        output=args.output,
        restart=args.restart,
        headed=args.headed,
        verbose=args.verbose,
    )


def _page_range(value: str) -> tuple[int, int | None]:
    value = value.strip().lower()
    if value == 'all':
        return 1, None
    if value.isdigit() and int(value) > 0:
        return 1, int(value)
    match = re.fullmatch(r'(\d+)-(\d*)', value)
    if match and int(match.group(1)) > 0:
        first, last = int(match.group(1)), int(match.group(2)) if match.group(2) else None
        if last is None or last >= first:
            return first, last
    raise argparse.ArgumentTypeError(f'expected "all", N or A-B, got {value!r}')


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be at least 1')
    return number


def setup_logging(verbose: bool) -> None:
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(errors='backslashreplace')  # titles may not fit the console code page
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format='%(asctime)s %(levelname)-7s %(message)s',
        datefmt='%H:%M:%S',
    )
    for noisy in ('asyncio', 'websocket', 'urllib3'):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def is_wanted_request(url: str, resource_type: str, main_frame: bool) -> bool:
    """Both crawlers load only each page's own HTML and the Disqus comment-count
    scripts. Everything else fails at once, so a dead third-party host cannot hold
    up a page, while the page's inline scripts still run."""
    return (main_frame and resource_type.lower() == 'document') or DISQUS_COUNTS in url


def backoff(attempt: int) -> float:
    return min(30.0, 2.0**attempt) + random.uniform(0, 1)


def desktop_user_agent(user_agent: str) -> str:
    """Headless Chrome announces itself as "HeadlessChrome", which bot filters block."""
    return user_agent.replace('HeadlessChrome/', 'Chrome/')


class RateLimiter:
    """Lets at most one page load start every ``interval`` seconds (thread-safe)."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            time.sleep(start - now)


class AsyncRateLimiter:
    """asyncio version of :class:`RateLimiter`."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._next = 0.0

    async def wait(self) -> None:
        now = time.monotonic()
        start = max(now, self._next)
        self._next = start + self.interval
        if start > now:
            await asyncio.sleep(start - now)


class CrawlRun:
    """Checkpoint, failures and progress of one crawl (thread-safe)."""

    def __init__(self, options: Options) -> None:
        self.options = options
        self.checkpoint = Checkpoint(options.checkpoint, resume=not options.restart)
        self.incomplete: dict[str, BlogPost] = {}  # written to the CSV, retried on the next run
        self.failed_pages: list[int] = []
        self.new_posts = 0
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._pages_done = 0

    def todo(self, teasers: list[Teaser]) -> list[tuple[int, Teaser]]:
        return [(position, t) for position, t in enumerate(teasers) if t.url not in self.checkpoint]

    def record(self, teaser: Teaser, article: Article | None, page: int, position: int) -> None:
        post = BlogPost.merge(teaser, article, page, position)
        if article is None or post.comment_count is None:
            with self._lock:
                self.incomplete[post.url] = post
            return
        self.checkpoint.add(post)
        with self._lock:
            self.incomplete.pop(post.url, None)
            self.new_posts += 1

    def page_failed(self, number: int, error: Exception) -> None:
        log.error('Listing page %d skipped: %s', number, error)
        with self._lock:
            self.failed_pages.append(number)

    def page_done(self, listing: Listing, last: int, crawled: int) -> None:
        with self._lock:
            self._pages_done += 1
            done, remaining = self._pages_done, last - listing.number
        elapsed = time.monotonic() - self._started
        eta = elapsed / done * remaining
        log.info(
            'Page %d/%d: %d posts, %d fetched | total %d saved, %d incomplete | %.0f posts/min | ETA %s',
            listing.number,
            last,
            len(listing.teasers),
            crawled,
            len(self.checkpoint),
            len(self.incomplete),
            self.new_posts / elapsed * 60 if elapsed else 0,
            _duration(eta),
        )

    def finish(self) -> int:
        """Write the CSV; exit status 0 if every post is complete, 1 otherwise."""
        self.checkpoint.close()
        posts = dict(self.checkpoint.posts)
        for url, post in self.incomplete.items():
            posts.setdefault(url, post)
        if not posts:
            log.error('Nothing was crawled')
            return 2
        rows = write_csv(posts.values(), self.options.output)
        _summary(posts.values(), rows, self.options.output, time.monotonic() - self._started)
        if self.incomplete or self.failed_pages:
            log.warning(
                '%d posts are incomplete and %d listing pages failed (%s); run the same command again to retry them',
                len(self.incomplete),
                len(self.failed_pages),
                ', '.join(map(str, self.failed_pages)) or 'none',
            )
            return 1
        return 0


def _summary(posts: Iterable[BlogPost], rows: int, path: Path, elapsed: float) -> None:
    posts = list(posts)
    log.info(
        'Wrote %d posts to %s in %s: %d without content, %d without comment count, %d images, %d comments',
        rows,
        path,
        _duration(elapsed),
        sum(not p.content for p in posts),
        sum(p.comment_count is None for p in posts),
        sum(len(p.images) for p in posts),
        sum(p.comment_count or 0 for p in posts),
    )


def _duration(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h{minutes:02d}m' if hours else f'{minutes}m{seconds:02d}s'
