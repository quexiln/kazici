# kazici

**A web scraper that learns a site once with an LLM, then runs without one.**

Give it a page URL and describe the data you want in plain language: "every book's link, title and price". kazici
inspects the page, has Claude write a site-specific extraction rule (CSS/XPath selectors, pagination, validation
criteria) and refines that rule by testing it on the live page. The approved rule is saved as a JSON file. Every run
after that **uses no LLM**: it is fast, free and deterministic.

```text
python -m kazici learn "https://books.toscrape.com/catalogue/category/books_1/index.html" "every book's link, title and price" --name books-toscrape
  → writes rules/books-toscrape.json

python -m kazici run "https://books.toscrape.com/catalogue/category/books_1/index.html" --all
  → follows every page and writes the items to output/, no LLM
```

> Goals can be written in any language; field names follow the language of the goal.

## Features

- **5 pagination types**: URL parameter (`?page=2`), "next" link, "load more" button, infinite scroll, and single
  pages. If page links are not in the DOM, the parameter is learned from the requests the site makes in the
  background.
- **Record (detail) pages**: if a requested value is not on the list page (e.g. a UPC code or description), each
  record's own page is opened and fields are extracted there too.
- **Search-driven scraping**: if records can only be reached through a search, the site's search is learned. Search
  terms can be changed later. Without a specific term, the system enumerates "everything" using an empty search,
  dropdown options or letter prefixes.
- **Self-checking**: every run checks item counts, field fill rates, value patterns and whether pagination brought
  new items. If the site changes and the rule breaks, the result is flagged `PROBLEM`.
- **Fast mode**: if plain HTTP (no browser) returns the same data, the rule switches to HTTP automatically.
- **Polite crawling**: robots.txt and Crawl-delay are respected, and requests to the same host are queued.
- **Web UI**: learn, run, edit rules, browse results as a table and download JSON/CSV.

## How it works

1. **Simplify.** The page is opened with Playwright. Scripts, styles, hidden elements and noisy attributes are
   removed, and repeating cards are reduced to 2 samples (typically ~50,000 → ~3,000 characters). Hints are added:
   repeating-region candidates with their real match counts; pagination, "load more" and cookie buttons; count hints
   such as "1,234 products"; whether scrolling adds items; and page-like network requests (`?pg=2`).
2. **Draft.** Claude Haiku 5.5 writes a JSON rule from this summary: setup steps (such as closing a cookie banner),
   the item selector, fields, the pagination type and validation criteria (items per page, expected total, required
   fields). Every piece of requested data is mapped to a field; a request that is not on the page is reported as a
   warning, not an error.
3. **Trial and error.** The rule is run on the live page across a few pages and goes through strict checks. Results
   and sample data are sent back to the LLM, which either accepts the rule or revises it. After 4 failed rounds it
   escalates to Sonnet 5.5 (at most 8 calls by default).
4. **Pin.** The approved rule is written to `rules/<name>.json`. If HTTP returns the same data, `fetch: http` is set.
   An estimated run time is shown at the end of learning.
5. **Run.** `run` executes the rule without an LLM on one or many pages and writes the result as JSON under
   `output/`. The rule is also picked up automatically for other pages that match its URL pattern (e.g. other
   categories).

A typical learning session takes 2 Haiku calls of 15–30 seconds each; most of the time is spent loading test pages.

## Requirements

- Python 3.12. Developed and tested on Windows 11; the code is not platform-specific, but the launcher and shortcut
  scripts are Windows-only.
- [Claude Code](https://claude.com/claude-code) installed and logged in. LLM calls go through your **Claude Code
  subscription** via the Claude Agent SDK, so no API key is needed. If `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`
  is set, the SDK would use the key instead of the subscription, so kazici removes these variables inside its own
  process.
- Running a rule (`run`) needs no LLM; Claude is only used by `learn`.

## Installation

```bash
git clone https://github.com/quexiln/kazici.git
```

```bash
cd kazici
```

```bash
pip install -r requirements.txt
```

```bash
python -m playwright install chromium
```

If you are not logged in to Claude Code yet, run `claude` and type `/login`.

If non-ASCII characters look garbled in the Windows console, set `PYTHONIOENCODING=utf-8`. Output files are always
UTF-8.

## Usage

### Learning a rule

```bash
python -m kazici learn "https://books.toscrape.com/catalogue/category/books_1/index.html" "every book's link, title and price" --name books-toscrape
```

| Option | Meaning |
|---|---|
| `--name` | Rule name (file name) |
| `--also URL` | Require the rule to also work on another page with the same layout (repeatable) |
| `--max-iter N` | Maximum LLM calls (default 8) |
| `--test-pages N` | Pages to test while learning (default 3) |
| `--search` / `--no-search` | Force / disable search; by default it is used only when needed |
| `--show-browser` | Show the browser window |
| `--model` | Starting model (default `claude-haiku-5-5`) |

### Running a rule

The rule is found automatically from the URL:

```bash
python -m kazici run "https://books.toscrape.com/catalogue/category/books_1/index.html"
```

Without a page limit the rule's own limit is used. `--max-pages N` sets a custom limit. `--all` (or
`--max-pages 0`) continues until the list ends naturally:

```bash
python -m kazici run "https://books.toscrape.com/catalogue/category/books_1/index.html" --all
```

For many pages, pass a file with one URL per line (lines starting with `#` are skipped):

```bash
python -m kazici run -f urls.txt --rule books-toscrape -o output/books.json
```

Other options: `--concurrency N` (URLs processed at once, default 2), `--show-browser`, `--ignore-robots`.

Each result has a status:

| Status | Meaning |
|---|---|
| `OK` | The list ended naturally and checks passed |
| `AT_LIMIT` | The page or query limit was reached; there may be more (use `--all`) |
| `PROBLEM` | Checks failed; the site may have changed and the rule may be broken. Issues are listed |
| `ROBOTS_BLOCKED` | robots.txt disallows this URL; no request was made |

Small categories, single-page lists and products without a price are not treated as errors: run mode applies looser
checks than learning mode.

### Output format

```json
{
  "created": "2026-01-01T12:00:00",
  "total_items": 1000,
  "results": [
    {
      "url": "https://books.toscrape.com/catalogue/category/books_1/index.html",
      "rule": "books-toscrape",
      "status": "OK",
      "total_unique": 1000,
      "fetch": "http",
      "stop_reason": "...",
      "fill_rates": {"url": 1.0, "title": 1.0, "price": 1.0},
      "issues": [],
      "pages": [{"page": 1, "url": "...", "matched": 20, "new": 20}],
      "items": [{"url": "...", "title": "...", "price": 12.5}]
    }
  ]
}
```

The web UI can also export the same result as a `;`-separated, Excel-friendly CSV.

### Other commands

```bash
python -m kazici rules
```

```bash
python -m kazici show books-toscrape
```

```bash
python -m kazici simplify "https://..." -o simplified.txt
```

`rules` lists saved rules, `show` prints a rule's JSON, and `simplify` writes the simplified view that is sent to the
LLM (for debugging).

## Record pages (detail step)

If a requested value is still missing from the list page, the first record's link is opened and a separate field
rule is learned for that page. The rule is tested on 3 record pages and reviewed by the LLM (at most 4 extra calls);
if HTTP gives the same result, detail pages are fetched without a browser too.

When running, the page of **every** record from the list is opened; the page limit applies only to list pages. For
that reason an estimated duration is shown at the end of learning and at the start of each run. Detail fields are
written only to records whose page opened successfully. If more than 20% of record pages fail to open, the result is
marked `PROBLEM`.

## Learning through search

Some sites don't list records and only offer search. Sometimes the requested data is itself a search result ("teams
with Boston in their name"). In these cases the rule is learned to use the site's search:

1. Search boxes and dropdowns inside search forms are detected. If the request needs a search, the LLM says which
   field to use and which terms to search for.
2. A probe search with a nonsense term is run in the browser. If the result URL contains the term, the rule stores a
   **URL template** (`...?q={query}`) and usually runs over HTTP. If the URL doesn't change (AJAX or a POST form),
   the search is done in the browser by **filling the form**.
3. The list rule is learned on a results page. If the term returns few results, pagination wouldn't be visible, so
   the rule is learned on a broad query and then tested with the term.

Terms are not baked into the rule; `{query}` is a parameter:

- **If the request names a term** ("videos titled 'cat'"), it is stored as the default term; other terms can be
  passed at run time.
- **If there is no specific term** ("name and link of every profile"), the system enumerates everything itself. It
  tries the following in order and stores the first that works:
  1. an empty search or `*`, if it lists everything;
  2. if the search is a dropdown (author, category, city…), every option one by one;
  3. otherwise letter prefixes (`a`, `b`, …, including Turkish letters such as `ç`, `ğ`, `ş`). A prefix that hits
     the result cap is extended by one letter (`a` → `aa`, `ab`, …).

Results are de-duplicated by the `unique_by` field, and each record gets the first query that found it as `_query`.
Because enumeration can issue many queries there is a query limit (default 200, `--max-queries 0` = unlimited). The
page limit applies to each query separately.

```bash
python -m kazici learn "https://www.scrapethissite.com/pages/forms/" "name, year and wins of teams with Boston in their name" --name s-hockey
```

A search rule needs no URL:

```bash
python -m kazici run --rule s-hockey -q Boston -q Rangers
```

```bash
python -m kazici run --rule all-profiles --queries terms.txt --max-queries 0
```

A results URL (`…?q=python`) can also be given; it is converted into a term. For dropdown searches, `-q` accepts the
option's visible label.

## Web UI

```bash
python -m kazici web
```

Opens `http://127.0.0.1:8765/` in the browser (options: `--port`, `--host`, `--no-open`). Light, dark and system
themes can be switched from the bottom left.

| Page | Contents |
|---|---|
| **Run** | Enter a list of URLs; the matching rule is shown as you type. Choose the page limit, concurrency and output name. For search rules, enter terms and a query limit; the URL can stay empty |
| **Learn** | Learn a rule from a sample page and a goal. "Simplified view" shows the summary the LLM sees |
| **Rules** | Rule summary, fields and a JSON editor (validated against the schema on save, Ctrl+S saves) |
| **Results** | Files in `output/`: fill-rate bars, issues, a searchable and sortable table, JSON/CSV download |
| **Jobs** | Live log and cancel. Jobs are kept in memory and the list resets when the server stops (files remain) |

By default the server is only reachable from this computer (`127.0.0.1`), and requests coming from other sites are
rejected. The **Stop server** button at the bottom left shuts it down, asking first if jobs are
running.

### Windows launcher and shortcuts

`kazici_launcher.pyw` runs without a console window: if the server is not running it starts it in the background,
then opens the UI in the browser. Server output goes to `kazici-web.log`.

```bash
python install_shortcuts.py
```

Installs a "kazici" shortcut on the desktop and in the Start menu. `--startup` also starts the server silently at
Windows startup, and `--uninstall` removes all shortcuts.

## Rule format

Rules are plain JSON and can be edited by hand. A shortened example:

```json
{
  "name": "books-toscrape",
  "site": "books.toscrape.com",
  "url_pattern": "^https?://(www\\.)?books\\.toscrape\\.com/catalogue",
  "fetch": "http",
  "setup": [{"action": "click", "selector": {"kind": "css", "value": "#cookie-reject"}, "optional": true}],
  "item": {"kind": "css", "value": "article.product_pod"},
  "fields": [
    {"name": "url",   "selector": {"kind": "css", "value": "h3 a"}, "attr": "href", "transforms": ["strip", "absolute_url"], "required": true},
    {"name": "price", "selector": {"kind": "css", "value": "p.price_color"}, "attr": "text", "transforms": ["strip", "number"]}
  ],
  "pagination": {"type": "next_button", "selector": {"kind": "css", "value": "li.next > a"}, "max_pages": 50},
  "expect": {"min_items_per_page": 18, "min_total": 900, "unique_by": "url", "field_patterns": {"url": "^https?://"}}
}
```

- Selectors can be CSS or XPath (`kind`).
- Pagination types: `none`, `url_param`, `next_button`, `load_more`, `infinite_scroll`.
- The `number` transform understands both Turkish and English number formats ("1.299,90" → 1299.9).
- Optional sections: `detail` (record page fields) and `search` (search settings).

See [kazici/rules.py](kazici/rules.py) for the full schema. Learned rules are saved to the `rules/` folder, which is
created on first use.

## Known limitations

- The detail step is one level deep: links on record pages are not followed.
- Dependent selections (pick an author, then a tag) are not supported.
- Sites that show a different result layout from visit to visit are partly supported: selectors
  are fitted to both layouts, but pagination can only be learned from one.
- Prefix enumeration doesn't guarantee full coverage: if search matches whole words, prefixes may miss records.
- When product and category pages share the same URL shape (e.g. `/slug`), the URL pattern only matches the learned
  category; run other categories with `--rule`.
- No attempt is made to bypass CAPTCHAs or bot protection.

## Responsible use

kazici respects robots.txt, waits for Crawl-delay (up to 60 s) and queues requests to the same host at the rule's
`delay_ms` interval. Use `--ignore-robots` only on sites you have permission to crawl. You are responsible for using
scraped data in line with the site's terms of service and applicable data-protection law.

## Project layout

| File | Role |
|---|---|
| `kazici/simplify.py` | DOM simplification; repeating-region, button and search-field detection |
| `kazici/rules.py` | Rule schema (pydantic), saving and finding a rule by URL |
| `kazici/llm.py` | Claude Agent SDK calls (subscription, structured JSON output) |
| `kazici/learner.py` | Trial-and-error learning loop, search and detail learning |
| `kazici/executor.py` | LLM-free rule execution, 5 pagination types, search |
| `kazici/extract.py` | Field extraction and transforms with parsel |
| `kazici/validate.py` | Strict checks and reports |
| `kazici/browser.py` | Playwright, HTTP, robots.txt, request throttling |
| `kazici/service.py` | Shared run/save logic for the CLI and the web UI |
| `kazici/web.py`, `kazici/static/` | Web UI (Starlette + build-free plain JS) |
| `kazici/cli.py` | Command line (typer) |

## License

[MIT](LICENSE)
