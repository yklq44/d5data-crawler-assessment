# MIT Admissions Blogs Crawler

Crawls every post on [mitadmissions.org/blogs](https://mitadmissions.org/blogs/) (about 7,900 posts written between 2004 and today) with **Playwright** and with **DrissionPage**, and saves one CSV row per post:

`Title | Author | Comment Count | Time | Article Content | Images In Article`

Both crawlers share one parser and both were run over the entire blog. Their CSV files are byte-for-byte identical; the [Results](#results) section has the details.

## Results

| | Playwright | DrissionPage |
|---|---:|---:|
| Posts | 7,864 | 7,864 |
| Listing pages | 492 | 492 |
| Run time | 37 min 20 s | 36 min 48 s |
| Incomplete posts | 0 | 0 |

Both crawls ran on 17 November 2026.

- **Full CSV files:** attached to the [latest release](https://github.com/yklq44/d5data-crawler-assessment/releases/latest) as `mit_blogs_playwright.csv.zip` and `mit_blogs_drissionpage.csv.zip`.
- **Samples:** [`samples/`](samples/) holds the first two listing pages (32 posts) from each crawler, small enough to preview on GitHub.
- **Cross-check:** the two CSV files are byte-for-byte identical (SHA-256 `d9c15ec7…8376`). `compare.py`, which matches posts by URL and compares every column, finds all 7,864 posts in both files and no difference.

The dataset has 191 authors, 124,402 comments and 35,894 image URLs. The longest post, "Two hundred puzzles", is 151,652 characters long. 46 posts have no text: 39 consist only of images, which are listed in Images In Article, and 7 only of a video (YouTube, an MP4 file or a Flash player), which none of the columns covers.

## CSV format

The six required columns come first, followed by three extra columns.

| Column | Content |
|---|---|
| Title | Post title, without the author byline that shares its heading. |
| Author | Blogger name as printed on the post, for example `Suzuko O. '30`. |
| Comment Count | Number of comments, read from Disqus or WordPress depending on the post's age (see [Comment counts](#comment-counts)). Posts whose comments were closed have `0`. |
| Time | Publication time in ISO 8601, in US Eastern Time (the site's time zone), for example `2026-11-16T21:51:07-05:00`. |
| Article Content | Plain text of the post body. Paragraphs are separated by a blank line, list items start with `- ` or `1. `, footnote markers are written `[1]`, and the footnotes follow the text under `Footnotes:`. Image captions are kept. Scripts, embedded players, hidden notices and carousel buttons are left out. |
| Images In Article | URLs of the images in the post body, one per line, in reading order. Gallery slides are included; emoji and smileys are not. Each URL is the image the page displays (its `src`, usually a resized copy made by WordPress). |
| URL | Address of the post. |
| Subtitle | Subtitle shown under the title (often empty on old posts). |
| Categories | Category names separated by `; `. |

The file is UTF-8 with a byte order mark, so Excel detects the encoding. Fields are comma-separated and quoted per RFC 4180, and cells may contain line breaks. Rows follow the site's order, newest first.

Excel limits a cell to 32,767 characters. 32 posts are longer, so Excel cuts their Article Content short; pandas and Python's `csv` module read the full text.

## How it works

### Sources

1. **Listing pages** `https://mitadmissions.org/blogs/page/N/`: 16 posts per page, 492 pages. Each card gives the post URL, title, subtitle, author, date and comment information.
2. **Post pages**: title, author, subtitle, categories, the exact publication time (the `datePublished` field of the page's JSON-LD), the body text and its images.

### Comment counts

The blog has used three comment systems, so the count is resolved per post:

| Posts from | The listing card shows | How the count is read |
|---|---|---|
| ~2010 to today | An empty link with a `data-disqus-identifier`, filled in later by Disqus' `count.js` | The browser runs `count.js`, which asks Disqus for up to 10 threads per request (`count-data.js?1=<id>&1=<id>…`). The crawler captures these responses and matches the thread ids. A thread that was asked for but is missing from the answer has no comments, so it gets `0`. |
| ~2005 to ~2010 | Static text such as "44 comments" | Parsed from the HTML. |
| 2004 to 2005 | Nothing; comments are closed | `0`. |

This is where the browsers earn their keep: the Disqus counts are not in the HTML, and a script on the page fetches them.

### One parser, two ways of fetching

The `mitblogs` package holds everything that does not depend on a browser:

- lxml parsers for listing pages, post pages and Disqus answers;
- the HTML-to-text renderer;
- the CSV writer and the checkpoint file;
- command-line options, rate limiting and progress reporting.

It is unit-tested against pages saved in `tests/fixtures`. Each crawler only drives the browser and hands the rendered HTML to the parser. As a result, the two CSV files can be compared field by field, and any difference points to a fetching problem rather than to two parsers disagreeing.

| | Playwright (`crawl_playwright.py`) | DrissionPage (`crawl_drissionpage.py`) |
|---|---|---|
| Concurrency | asyncio: a pool of pages | Threads: one Chromium and a pool of tabs, each tab used by one thread at a time |
| Request filtering | `context.route` | CDP request interception: `Fetch.enable` pauses every request, and a callback answers `Fetch.continueRequest` or `Fetch.failRequest` |
| Disqus counts | `page.on('response')` | `tab.listen`, the DrissionPage network listener |
| Page loading | `goto(wait_until='domcontentloaded')`, then `page.content()` | Post tabs use the `none` load mode, then read the HTML with a single `Runtime.evaluate` in the page once `DOMContentLoaded` has been handled. The listing tab uses the normal mode, so that `count.js` gets to run. |

Both crawlers download the next listing page while the posts of the current one are loading.

Both apply the same rule, `is_wanted_request` in `mitblogs/crawl.py`: a page loads its own HTML and the Disqus count scripts, and every other request fails at once (images, styles, fonts, external scripts, analytics, embedded players). JavaScript stays on, because a few posts build part of their content with inline scripts. External scripts, on the other hand, can no longer hold up a page. Some old posts embed synchronous scripts from hosts that no longer answer (Kaltura video players), and such a script would block `DOMContentLoaded` until the browser gave up on it.

### Politeness and robustness

- `robots.txt` allows `/blogs/`; it only disallows `/wp-admin/` and a forms upload folder.
- Each crawler starts at most one page load every 0.25 s and keeps 4 posts in flight. Only the HTML is downloaded, so a post costs a single request.
- Each page gets up to 3 attempts with exponential backoff. A 404 is not retried. Error and challenge pages are detected because they lack the article body, and are retried like timeouts.
- When Disqus does not answer, the listing page is loaded again (up to 3 attempts) before its posts are marked incomplete.
- Finished posts are appended to a JSON Lines checkpoint next to the CSV. Running the same command again continues where the last run stopped and retries the posts that failed. Ctrl+C still writes the CSV for the posts crawled so far.
- The exit status is 0 when every post is complete, 1 when some posts or pages are incomplete (run again to retry them), and 2 when nothing was crawled.

### Problems found along the way

Most of these showed up only on the full crawl, which is why both crawlers were run over the whole blog.

- **Comment counts are not in the HTML.** Disqus fills them in with a script, so the crawlers capture the script's own requests rather than reading the page.
- **DrissionPage's eager load mode cancelled `count.js`.** Eager mode stops loading at `DOMContentLoaded`, before the asynchronous Disqus script has run. The listing tab therefore uses the normal load mode.
- **Eager mode also aborted later page loads.** Each navigation starts a timer thread that calls `Page.stopLoading` when the page load timeout runs out, even if the tab has moved on to another page by then. That caused about 20 random "page did not load" retries in the first full run. Post tabs now use the `none` load mode and wait for `DOMContentLoaded` themselves.
- **Dead embeds stalled old posts.** DrissionPage first blocked requests with `Network.setBlockedURLs` patterns, which only cover the hosts listed in advance. A post with five Kaltura players timed out three times, because their synchronous scripts held back `DOMContentLoaded`. DrissionPage now intercepts every request and applies the same rule as the Playwright route handler, so unknown hosts fail at once.
- **A stale document stopped a run.** After 2,558 posts, DrissionPage read an empty page. `tab.html` serializes a document node that DrissionPage caches and updates from page events; when an event of the previous page is handled late, that node can be stale or empty. lxml's error was not a parse error, so it escaped the retry loop and stopped the crawl. The same race could also have returned the previous post; none had, as the comparison with Playwright showed. The crawler now reads the HTML with one `Runtime.evaluate` in the live page once `DOMContentLoaded` has been handled, and marks each document as read. Unreadable pages are retried, and an unexpected error in one post is logged without stopping the crawl.
- **Turning JavaScript off lost content.** An intermediate version loaded posts with JavaScript disabled. Compared with the previous full run, "The Periodic Table of MIT" came out without its table, which an inline script builds. Filtering requests keeps inline scripts running and still stops the dead embeds.
- **E-mail addresses were hidden.** Cloudflare replaces addresses with `[email protected]` and decodes them with an external script. With that script blocked, 183 posts in the first Playwright run contained the placeholder, while DrissionPage, which let it run, had the real addresses. The parser now decodes the `data-cfemail` attribute itself, so both crawlers produce the real addresses.
- **One post is longer than Python's default CSV field limit.** "Two hundred puzzles" is about 150,000 characters, above the default limit of 131,072. `compare.py` and the tests raise the limit before reading.

## Usage

You need Python 3.10 or newer and Google Chrome. Without Chrome, Playwright falls back to its own Chromium, which `playwright install chromium` downloads.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python crawl_playwright.py       # whole blog -> output/mit_blogs_playwright.csv
python crawl_drissionpage.py     # whole blog -> output/mit_blogs_drissionpage.csv
python compare.py output/mit_blogs_playwright.csv output/mit_blogs_drissionpage.csv
```

Both crawlers accept the same options:

| Option | Default | Meaning |
|---|---|---|
| `--pages` | `all` | Listing pages to crawl: `N` for the first N pages, `A-B` for a range. |
| `--concurrency` | `4` | Posts loaded in parallel. |
| `--delay` | `0.25` | Minimum seconds between two page loads. |
| `--timeout` | `45` | Page load timeout in seconds. |
| `--retries` | `3` | Attempts per page. |
| `--output` | `output/mit_blogs_<tool>.csv` | CSV file to write; the checkpoint goes next to it. |
| `--restart` | | Ignore the checkpoint of a previous run. |
| `--headed` | | Show the browser window. |
| `-v` | | Log every post. |

For example, `python crawl_drissionpage.py --pages 3 --headed` crawls the 48 newest posts in a visible browser.

### Tests

```bash
pytest                                   # offline, runs against tests/fixtures
ruff check . && ruff format --check .
```

GitHub Actions runs both on every push. The "Crawl with both tools and compare" job can be started by hand from the Actions tab; it crawls a few listing pages with each tool, compares the results and uploads the CSV files.

## Project layout

```
crawl_playwright.py      Playwright crawler (asyncio)
crawl_drissionpage.py    DrissionPage crawler (threads and tabs)
compare.py               cross-checks two CSV files by URL
mitblogs/
  parse.py               listing, post and Disqus parsers
  text.py                HTML to plain text
  models.py              Teaser, Article and BlogPost records; CSV columns
  store.py               CSV writer and JSON Lines checkpoint
  crawl.py               options, rate limiting, retries and progress
tests/                   pytest suite and saved pages
samples/                 first two listing pages from each crawler
```
