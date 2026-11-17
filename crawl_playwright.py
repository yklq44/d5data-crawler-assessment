"""Crawl https://mitadmissions.org/blogs/ with Playwright (async API).

One browser context: a page walks the listing and captures the Disqus
comment-count responses it triggers, while a pool of pages downloads the posts
concurrently. A route handler lets through only each page's own HTML and the
Disqus count scripts. The next listing page is prefetched while posts are
downloading.

Usage: python crawl_playwright.py [--pages all|N|A-B] [--concurrency 4] [--delay 0.25]
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Callable
from typing import TypeVar

from playwright.async_api import Browser, BrowserContext, Page, Playwright, Response, Route, async_playwright
from playwright.async_api import Error as PlaywrightError

from mitblogs.crawl import (
    DISQUS_WAIT,
    AsyncRateLimiter,
    CrawlError,
    CrawlRun,
    Listing,
    Options,
    PageGone,
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

log = logging.getLogger('crawl_playwright')
T = TypeVar('T')


async def only_html_and_comment_counts(route: Route) -> None:
    request = route.request
    if is_wanted_request(request.url, request.resource_type, main_frame=request.frame.parent_frame is None):
        await route.continue_()
    else:
        await route.abort()


class PlaywrightCrawler:
    def __init__(self, context: BrowserContext, run: CrawlRun) -> None:
        self.context = context
        self.run = run
        self.options = run.options
        self.limiter = AsyncRateLimiter(self.options.delay)

    async def crawl(self) -> None:
        options = self.options
        listing_page = await self.context.new_page()
        pages: asyncio.Queue[Page] = asyncio.Queue()
        for _ in range(options.concurrency):
            pages.put_nowait(await self.context.new_page())

        number = options.first_page
        listing: Listing | None = await self.listing(listing_page, number)
        last = options.stop_page(listing.last_page)
        while True:
            upcoming = asyncio.create_task(self.listing(listing_page, number + 1)) if number < last else None
            if listing is not None:
                todo = self.run.todo(listing.teasers)
                await asyncio.gather(*(self.post(pages, number, position, teaser) for position, teaser in todo))
                self.run.page_done(listing, last, len(todo))
            if upcoming is None:
                break
            number += 1
            try:
                listing = await upcoming
            except CrawlError as exc:
                self.run.page_failed(number, exc)
                listing = None

    async def fetch(self, page: Page, url: str, parse: Callable[[str], T]) -> T:
        """Load ``url`` and parse the rendered HTML, retrying with backoff."""
        error: object = None
        for attempt in range(1, self.options.retries + 1):
            await self.limiter.wait()
            try:
                response = await page.goto(url, wait_until='domcontentloaded', timeout=self.options.timeout * 1000)
                html = await page.content()
            except PlaywrightError as exc:
                error = exc.message.splitlines()[0]
            else:
                status = response.status if response else 0
                if status == 404 or is_not_found(html):
                    raise PageGone(f'{url}: HTTP 404')
                if 200 <= status < 400:
                    try:
                        return parse(html)
                    except ParseError as exc:
                        error = exc
                else:
                    error = f'HTTP {status}' if status else 'no response'
            if attempt < self.options.retries:
                pause = backoff(attempt)
                log.warning('%s: %s; retrying in %.0fs', url, error, pause)
                await asyncio.sleep(pause)
        raise CrawlError(f'{url}: {error}')

    async def listing(self, page: Page, number: int) -> Listing:
        """Read a listing page with its comment counts, reloading it while Disqus does not answer."""
        for attempt in range(1, self.options.retries + 1):
            listing, unknown = await self.load_listing(page, number)
            if not unknown or attempt == self.options.retries:
                break
            pause = backoff(attempt)
            log.warning(
                'Page %d: no comment count from Disqus for %d posts; reloading in %.0fs', number, len(unknown), pause
            )
            await asyncio.sleep(pause)
        if unknown:
            log.warning('Page %d: no comment count from Disqus for %d posts', number, len(unknown))
        return listing

    async def load_listing(self, page: Page, number: int) -> tuple[Listing, list[Teaser]]:
        """Read a listing page and the comment counts its Disqus script fetches."""
        responses: list[Response] = []

        def on_response(response: Response) -> None:
            if 'count-data.js' in response.url:
                responses.append(response)

        page.on('response', on_response)
        try:
            listing = await self.fetch(page, listing_url(number), lambda html: read_listing(html, number))
            return listing, await self.resolve_comment_counts(listing, responses)
        finally:
            page.remove_listener('response', on_response)

    async def resolve_comment_counts(self, listing: Listing, responses: list[Response]) -> list[Teaser]:
        """count.js asks Disqus for 10 threads per request; wait until every
        Disqus post of the page has been asked for (or give up). Returns the
        posts whose count is still unknown."""
        wanted = {t.disqus_id for t in listing.teasers if t.disqus_id and t.comment_count is None}
        counts: dict[str, int] = {}
        requested: set[str] = set()
        handled = 0
        deadline = time.monotonic() + DISQUS_WAIT
        while wanted - requested and time.monotonic() < deadline:
            if handled == len(responses):
                await asyncio.sleep(0.1)
                continue
            response = responses[handled]
            handled += 1
            try:
                body = await response.text() if response.ok else None
            except PlaywrightError:
                body = None
            parsed = parse_disqus_counts(body)
            if parsed is not None:
                counts |= parsed
                requested.update(disqus_request_ids(response.url))
        return apply_disqus_counts(listing.teasers, counts, requested)

    async def post(self, pages: asyncio.Queue[Page], number: int, position: int, teaser: Teaser) -> None:
        page = await pages.get()
        if page.is_closed():  # crashed renderer
            page = await self.context.new_page()
        article = None
        try:
            article = await self.fetch(page, teaser.url, lambda html: parse_article(html, teaser.url))
            log.debug('%s: %d chars, %d images', teaser.url, len(article.content), len(article.images))
        except PageGone as exc:
            log.warning('%s; keeping the listing data only', exc)
        except CrawlError as exc:
            log.error('Post skipped: %s', exc)
        except Exception:  # keep crawling; the post stays incomplete for the next run
            log.exception('Post skipped: %s', teaser.url)
        finally:
            pages.put_nowait(page)
        self.run.record(teaser, article, number, position)


async def launch(playwright: Playwright, options: Options) -> Browser:
    """Prefer the installed Google Chrome; fall back to Playwright's Chromium."""
    try:
        return await playwright.chromium.launch(channel='chrome', headless=not options.headed)
    except PlaywrightError:
        log.info('Google Chrome not found, using the Chromium build of Playwright')
        return await playwright.chromium.launch(headless=not options.headed)


async def crawl(options: Options, run: CrawlRun) -> None:
    async with async_playwright() as playwright:
        browser = await launch(playwright, options)
        try:
            probe = await browser.new_page()
            user_agent = desktop_user_agent(await probe.evaluate('navigator.userAgent'))
            await probe.close()
            context = await browser.new_context(user_agent=user_agent, locale='en-US', service_workers='block')
            await context.route('**/*', only_html_and_comment_counts)
            await PlaywrightCrawler(context, run).crawl()
        finally:
            await browser.close()


def main(argv: list[str] | None = None) -> int:
    options = parse_options('Playwright', argv)
    setup_logging(options.verbose)
    run = CrawlRun(options)
    completed = False
    try:
        asyncio.run(crawl(options, run))
        completed = True
    except KeyboardInterrupt:
        log.warning('Interrupted; saving the posts crawled so far')
    except CrawlError as exc:
        log.error('Crawl stopped: %s', exc)
    except Exception:
        log.exception('Crawl crashed; saving the posts crawled so far')
    status = run.finish()
    return status if completed else max(status, 1)


if __name__ == '__main__':
    sys.exit(main())
