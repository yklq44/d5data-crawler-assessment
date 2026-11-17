"""Crawl https://mitadmissions.org/blogs/ with DrissionPage.

One Chromium instance driven over CDP: a tab walks the listing and listens for
the Disqus comment-count responses, while a pool of tabs downloads the posts,
each tab used by one worker thread at a time. CDP request interception lets
through only each page's own HTML and the Disqus count scripts. The next
listing page is prefetched while posts are downloading.

Usage: python crawl_drissionpage.py [--pages all|N|A-B] [--concurrency 4] [--delay 0.25]
"""

from __future__ import annotations

import logging
import queue
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import CDPError, ContextLostError, PageDisconnectedError
from DrissionPage.items import MixTab

from mitblogs.crawl import (
    DISQUS_WAIT,
    CrawlError,
    CrawlRun,
    Listing,
    Options,
    PageGone,
    RateLimiter,
    backoff,
    desktop_user_agent,
    is_wanted_request,
    parse_options,
    read_listing,
    setup_logging,
)
from mitblogs.models import Teaser
from mitblogs.parse import (
    ParseError,
    apply_disqus_counts,
    disqus_request_ids,
    is_not_found,
    listing_url,
    parse_article,
    parse_disqus_counts,
)

log = logging.getLogger('crawl_drissionpage')
T = TypeVar('T')

# Evaluated in the live page: its HTML once DOMContentLoaded has been handled, or
# null. Each document is read once, so a page that has not been replaced yet by
# the next navigation is never returned twice.
READ_HTML = """(() => {
    const navigation = performance.getEntriesByType('navigation')[0];
    if (window.__crawlerRead || !navigation || !navigation.domContentLoadedEventEnd) return null;
    window.__crawlerRead = true;
    return document.documentElement.outerHTML;
})()"""


def only_html_and_comment_counts(tab: MixTab) -> None:
    """Pause every request of the tab with the CDP Fetch domain and fail the ones
    :func:`is_wanted_request` rejects. In a page target the main frame id is the
    target id."""

    def on_request_paused(**event: Any) -> None:
        wanted = is_wanted_request(event['request']['url'], event['resourceType'], event['frameId'] == tab.tab_id)
        try:
            if wanted:
                tab.run_cdp('Fetch.continueRequest', requestId=event['requestId'])
            else:
                tab.run_cdp('Fetch.failRequest', requestId=event['requestId'], errorReason='BlockedByClient')
        except (CDPError, PageDisconnectedError):
            pass  # the request was cancelled by a new navigation, or the tab closed

    tab.driver.set_callback('Fetch.requestPaused', on_request_paused, immediate=True)
    tab.run_cdp('Fetch.enable', patterns=[{'urlPattern': '*'}])


class DrissionCrawler:
    def __init__(self, browser: Chromium, run: CrawlRun) -> None:
        self.browser = browser
        self.run = run
        self.options = run.options
        self.limiter = RateLimiter(self.options.delay)
        self.user_agent = ''

    def crawl(self) -> None:
        options = self.options
        listing_tab = self.browser.latest_tab
        self.user_agent = desktop_user_agent(listing_tab.user_agent)
        self.prepare(listing_tab)
        listing_tab.set.load_mode.normal()  # wait for the load event, by which count.js has asked Disqus
        tabs: queue.Queue[MixTab] = queue.Queue()
        for _ in range(options.concurrency):
            tabs.put(self.new_tab())

        number = options.first_page
        listing: Listing | None = self.listing(listing_tab, number)
        last = options.stop_page(listing.last_page)
        executor = ThreadPoolExecutor(max_workers=options.concurrency + 1, thread_name_prefix='tab')
        try:
            while True:
                upcoming = executor.submit(self.listing, listing_tab, number + 1) if number < last else None
                if listing is not None:
                    todo = self.run.todo(listing.teasers)
                    posts = [executor.submit(self.post, tabs, number, pos, teaser) for pos, teaser in todo]
                    for future in posts:
                        future.result()
                    self.run.page_done(listing, last, len(todo))
                if upcoming is None:
                    break
                number += 1
                try:
                    listing = upcoming.result()
                except CrawlError as exc:
                    self.run.page_failed(number, exc)
                    listing = None
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def new_tab(self) -> MixTab:
        tab = self.browser.new_tab()
        self.prepare(tab)
        return tab

    def prepare(self, tab: MixTab) -> None:
        tab.set.user_agent(self.user_agent)
        only_html_and_comment_counts(tab)

    def fetch(self, tab: MixTab, url: str, parse: Callable[[str], T]) -> T:
        """Load ``url`` and parse the HTML once the DOM is ready, retrying with backoff."""
        error: object = None
        for attempt in range(1, self.options.retries + 1):
            self.limiter.wait()
            try:
                tab.get(url, retry=0, timeout=self.options.timeout, show_errmsg=True)
                html = self.read_html(tab)
            except Exception as exc:  # navigation error, timeout, or the tab crashed
                error = f'{type(exc).__name__}: {exc}'
            else:
                if is_not_found(html):
                    raise PageGone(f'{url}: HTTP 404')
                try:
                    return parse(html)
                except ParseError as exc:
                    error = exc
            if attempt < self.options.retries:
                pause = backoff(attempt)
                log.warning('%s: %s; retrying in %.0fs', url, error, pause)
                time.sleep(pause)
        raise CrawlError(f'{url}: {error}')

    def read_html(self, tab: MixTab) -> str:
        """The HTML of the page the tab is navigating to. ``tab.html`` reads the
        document node DrissionPage caches, which a late event of the previous page
        can leave pointing at that page or at a half-parsed one."""
        deadline = time.monotonic() + self.options.timeout
        while time.monotonic() < deadline:
            try:
                result = tab.run_cdp('Runtime.evaluate', expression=READ_HTML, returnByValue=True)
            except (CDPError, ContextLostError):  # between two documents
                result = {}
            html = result.get('result', {}).get('value')
            if isinstance(html, str):
                return html
            time.sleep(0.05)
        raise TimeoutError(f'DOMContentLoaded not reached in {self.options.timeout:.0f}s')

    def listing(self, tab: MixTab, number: int) -> Listing:
        """Read a listing page with its comment counts, reloading it while Disqus does not answer."""
        for attempt in range(1, self.options.retries + 1):
            listing, unknown = self.load_listing(tab, number)
            if not unknown or attempt == self.options.retries:
                break
            pause = backoff(attempt)
            log.warning(
                'Page %d: no comment count from Disqus for %d posts; reloading in %.0fs', number, len(unknown), pause
            )
            time.sleep(pause)
        if unknown:
            log.warning('Page %d: no comment count from Disqus for %d posts', number, len(unknown))
        return listing

    def load_listing(self, tab: MixTab, number: int) -> tuple[Listing, list[Teaser]]:
        """Read a listing page and the comment counts its Disqus script fetches."""
        tab.listen.start('count-data.js')
        try:
            listing = self.fetch(tab, listing_url(number), lambda html: read_listing(html, number))
            return listing, self.resolve_comment_counts(tab, listing)
        finally:
            tab.listen.stop()

    def resolve_comment_counts(self, tab: MixTab, listing: Listing) -> list[Teaser]:
        """count.js asks Disqus for 10 threads per request; wait until every
        Disqus post of the page has been asked for (or give up). Returns the
        posts whose count is still unknown."""
        wanted = {t.disqus_id for t in listing.teasers if t.disqus_id and t.comment_count is None}
        counts: dict[str, int] = {}
        requested: set[str] = set()
        if wanted:
            for packet in tab.listen.steps(timeout=DISQUS_WAIT):
                response = packet.response
                ok = not packet.is_failed and response is not None and response.status == 200
                parsed = parse_disqus_counts(response.raw_body if ok else None)
                if parsed is not None:
                    counts |= parsed
                    requested.update(disqus_request_ids(packet.url))
                if wanted <= requested:
                    break
        return apply_disqus_counts(listing.teasers, counts, requested)

    def post(self, tabs: queue.Queue[MixTab], number: int, position: int, teaser: Teaser) -> None:
        tab = tabs.get()
        article = None
        try:
            article = self.fetch(tab, teaser.url, lambda html: parse_article(html, teaser.url))
            log.debug('%s: %d chars, %d images', teaser.url, len(article.content), len(article.images))
        except PageGone as exc:
            log.warning('%s; keeping the listing data only', exc)
        except CrawlError as exc:
            log.error('Post skipped: %s', exc)
        except Exception:  # keep crawling; the post stays incomplete for the next run
            log.exception('Post skipped: %s', teaser.url)
        finally:
            tabs.put(tab if tab.states.is_alive else self.new_tab())
        self.run.record(teaser, article, number, position)


def browser_options(options: Options) -> ChromiumOptions:
    co = ChromiumOptions()
    co.auto_port()  # private profile on a free port: never attaches to the user's own Chrome
    co.headless(not options.headed)
    co.no_imgs(True)
    co.mute(True)
    # get() returns once the navigation has started and read_html() waits for DOMContentLoaded.
    # The eager mode would do the same, but it also stops loading from a timer thread that can
    # fire during a later navigation of the same tab and abort it.
    co.set_load_mode('none')
    co.set_timeouts(base=5, page_load=options.timeout)
    return co


def main(argv: list[str] | None = None) -> int:
    options = parse_options('DrissionPage', argv)
    setup_logging(options.verbose)
    run = CrawlRun(options)
    browser = Chromium(browser_options(options))
    completed = False
    try:
        DrissionCrawler(browser, run).crawl()
        completed = True
    except KeyboardInterrupt:
        log.warning('Interrupted; saving the posts crawled so far')
    except CrawlError as exc:
        log.error('Crawl stopped: %s', exc)
    except Exception:
        log.exception('Crawl crashed; saving the posts crawled so far')
    finally:
        browser.quit(del_data=True)
    status = run.finish()
    return status if completed else max(status, 1)


if __name__ == '__main__':
    sys.exit(main())
