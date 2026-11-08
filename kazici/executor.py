"""Runs a rule without the LLM: setup, pagination, extraction, deduplication."""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
from parsel import Selector

from . import browser as B
from .extract import extract_html, extract_page
from .rules import Detail, Rule, Search, fill_template, is_placeholder_option

Progress = Optional[Callable[[str], None]]

# Stop reasons meaning the pagination ended naturally
NATURAL_END = {"single_page", "no_new_items", "no_next_link", "no_next_button",
               "no_load_more_button", "nothing_loaded", "empty_page", "queries_done"}
ALL_PAGES = 0          # max_pages=0: no page limit
UNLIMITED = sys.maxsize
NEXT_TXT = re.compile(r"^\s*(?:(?:sonraki|next|ileri)(?:\s+(?:sayfa|page))?\s*[›»>→]*|[›»>→]+)\s*$", re.I)


@dataclass
class PageStat:
    page: int
    url: str
    matched: int
    new: int
    query: Optional[str] = None  # search rule: the query that produced this page
    rows: int = -1               # number of extracted (non-empty) rows; -1 = unknown


@dataclass
class RunResult:
    url: str
    items: list[dict] = field(default_factory=list)
    pages: list[PageStat] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    fetch_used: str = "browser"
    stop_reason: str = ""
    rounds: int = 0  # pages visited / click / scroll rounds
    delay_ms: int = 0
    detail_visited: int = 0   # record (detail) pages opened successfully; only these records have detail keys
    detail_failed: int = 0    # record pages that failed to open (independent of the error message cap)
    detail_sec_per_page: float = 0.0  # estimated time per record page (s), for warnings
    queries: list[dict] = field(default_factory=list)  # search rule: summary per query
    queries_left: int = 0  # queries not run because of the query limit (as far as known)
    search_failed: int = 0  # queries whose result page could not be read at all (independent of the error message cap)

    @property
    def first_page_count(self) -> int:
        return self.pages[0].matched if self.pages else 0

    @property
    def natural_end(self) -> bool:
        return self.stop_reason in NATURAL_END

    def summary(self) -> dict:
        out = {"url": self.url, "total_unique": len(self.items), "fetch": self.fetch_used, "rounds": self.rounds,
               "pages": [p.__dict__ for p in self.pages], "errors": self.errors, "stop_reason": self.stop_reason}
        if self.detail_visited:
            out["detail_visited"] = self.detail_visited
        if self.detail_failed:
            out["detail_failed"] = self.detail_failed
        if self.queries:
            out["queries"] = self.queries
            if self.queries_left:
                out["queries_left"] = self.queries_left
            if self.search_failed:
                out["search_failed"] = self.search_failed
        return out


class _Collector:
    def __init__(self, rule: Rule):
        self.rule = rule
        self.items: list[dict] = []
        self.keys: set[str] = set()

    def key(self, row: dict) -> str:
        k = self.rule.expect.unique_by
        if k and row.get(k) not in (None, "", []):
            return json.dumps(row[k], ensure_ascii=False)
        return json.dumps(row, ensure_ascii=False, sort_keys=True)

    def add(self, rows: list[dict]) -> int:
        n = 0
        for r in rows:
            k = self.key(r)
            if k not in self.keys:
                self.keys.add(k)
                self.items.append(r)
                n += 1
        return n


def set_param(url: str, param: str, value: int) -> str:
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != param]
    q.append((param, str(value)))
    return urlunparse(p._replace(query=urlencode(q)))


def page_url(url: str, rule: Rule, i: int) -> str:
    """URL of page i for url_param (0-based). Page 1 (i=0) = the given URL.
    If the URL already contains ?page=N, it continues from N."""
    if i == 0:
        return url
    pg = rule.pagination
    step = pg.step
    if step == 1 and pg.page2_value is not None and pg.page2_value > 2:
        step = pg.page2_value  # ?offset=20 style: step = page size
    cur = next((v for k, v in parse_qsl(urlparse(url).query) if k == pg.param), None)
    if cur is not None and cur.lstrip("-").isdigit():
        return set_param(url, pg.param, int(cur) + i * step)
    return set_param(url, pg.param, pg.second_value() + (i - 1) * step)


def page_urls(url: str, rule: Rule, limit: int) -> list[str]:
    """URLs of the first `limit` pages (for tests and debugging)."""
    return [page_url(url, rule, i) for i in range(limit)]


def _param_kept(url: str, rule: Rule) -> bool:
    return any(k == rule.pagination.param for k, _ in parse_qsl(urlparse(url).query))


def _page_number(url: str) -> Optional[int]:
    """Page number in the URL (query or /page/N/ style); None if it cannot be read."""
    p = urlparse(url)
    for k, v in parse_qsl(p.query):
        if v.isdigit() and _PAGE_KEY.fullmatch(k):
            return int(v)
    m = re.search(r"/(?:page|sayfa)[/-](\d+)", p.path, re.I)
    return int(m.group(1)) if m else None


_PAGE_KEY = re.compile(r"(page|sayfa|p|pg|pi|sf|pagenum|pageno|currentpage|page_?number|paged)", re.I)


def _is_current(me) -> bool:
    """Whether the link marks the current page (aria-current, active/current/selected class; on itself or its parent)."""
    if not me:
        return False
    cls = " ".join(me.xpath("./@class | ../@class").getall())
    aria = " ".join(me.xpath("./@aria-current | ../@aria-current").getall())
    return bool(re.search(r"\b(active|current|selected)\b", cls, re.I) or aria.strip() not in ("", "false"))


def next_href(html: str, base_url: str, rule: Rule, visited: set[str]) -> Optional[str]:
    """Next page link. If the selector matches several links: the one with 'next' text / rel=next;
    if a single candidate is left, that one; otherwise the smallest page number greater than the current
    page (if the current page cannot be read from the URL, the smallest n>=1 not marked 'active': in a
    0-based pager ?page=1 is the second page); otherwise the first link not visited yet."""
    sel = rule.pagination.selector
    if not sel:
        return None
    s = Selector(text=html)
    b = s.xpath("//base/@href").get()
    base = urljoin(base_url, b) if b else base_url
    try:
        nodes = s.xpath(sel.value) if sel.kind == "xpath" else s.css(sel.value)
    except Exception:
        return None
    cands = []
    for n in nodes:
        root = n.root
        if isinstance(root, str):  # @href selected: read the 'active' mark from the <a> with the same href
            href, preferred = root, False
            current = _is_current(s.xpath("//a[@href=$h][1]", h=root))
        else:
            href = (n.xpath("ancestor-or-self::a[@href][1]/@href").get()
                    or n.xpath(".//a[@href]/@href").get())
            a = n.xpath("ancestor-or-self::a[1]")
            txt = re.sub(r"\s+", " ", " ".join(n.xpath(".//text()").getall()))
            rel = (a.attrib.get("rel", "") if a else "") or (root.get("rel") or "")
            label = (a.attrib.get("aria-label", "") if a else "") + " " + (root.get("aria-label") or "")
            preferred = bool(NEXT_TXT.match(txt) or "next" in rel.split()
                             or re.search(r"\b(next|sonraki)\b", label, re.I))
            current = not preferred and _is_current(a if a else n)
        if href and not href.strip().startswith(("javascript:", "#")):
            cands.append((preferred, urljoin(base, href.strip()), current))
    seen, fresh = set(), []
    for p, h, c in cands:
        if h not in visited and h not in seen:
            seen.add(h)
            fresh.append((p, h, c))
    for p, h, _ in fresh:
        if p:
            return h
    cur = _page_number(base_url)
    if len(fresh) == 1 and not fresh[0][2]:
        n = _page_number(fresh[0][1])
        return None if cur is not None and n is not None and n < cur else fresh[0][1]  # do not go back
    nums = {h: (_page_number(h), c) for _, h, c in fresh}
    if any(n is not None for n, _ in nums.values()):
        # page numbers are readable: go to the smallest number greater than the current page, never back
        if cur is not None:
            ahead = sorted((n, h) for h, (n, _) in nums.items() if n is not None and n > cur)
        else:  # current page unknown (bare URL): smallest unvisited, non-active n>=1
            ahead = sorted((n, h) for h, (n, c) in nums.items() if n is not None and n >= 1 and not c)
        return ahead[0][1] if ahead else None
    return fresh[0][1] if fresh else None


async def run_rule(rule: Rule, url: str, session: Optional[B.BrowserSession] = None,
                   max_pages: Optional[int] = None, progress: Progress = None,
                   force_fetch: Optional[str] = None, respect_robots: bool = True,
                   queries: Optional[list[str]] = None, max_queries: Optional[int] = None) -> RunResult:
    """max_pages limits only the list pages (per query for a search rule); if the rule has a record page
    step, the page of every record from the list is opened. For a search rule, `queries` are searched if
    given, otherwise the rule's own terms or its enumeration (enum) method are used."""
    if rule.search is not None:
        res = await _run_search(rule, url, session, max_pages, progress, force_fetch, respect_robots,
                                queries, max_queries)
    else:
        res = await _run_list(rule, url, session, max_pages, progress, force_fetch)
    if rule.detail and res.items:
        try:
            await enrich(rule.detail, res, session, progress, respect_robots, rule.delay_ms)
        except Exception as e:
            res.errors.append(f"record pages error: {_err(e)}")
    return res


# Estimated time to open and read one record page (s); if shorter than the delay, the delay decides
PAGE_OPEN_S = {"http": 0.6, "browser": 3.0}


def fmt_duration(sec: float) -> str:
    if sec < 90:
        return f"~{max(1, round(sec))} s"
    if sec < 90 * 60:
        return f"~{round(sec / 60)} min"
    return f"~{sec / 3600:.1f} h"


async def detail_sec_per_page(detail: Detail, link: str, delay_ms: int) -> float:
    """Time per record page: requests to the same host are spaced by at least max(rule delay, robots Crawl-delay)."""
    gap = max(delay_ms, await B.robots_delay_ms(link)) / 1000
    return max(gap, PAGE_OPEN_S.get(detail.fetch, 3.0))


async def enrich(detail: Detail, res: RunResult, session: Optional[B.BrowserSession],
                 progress: Progress = None, respect_robots: bool = True, delay_ms: int = 1000) -> None:
    """Follows each record's own link ONE level down and adds the detail fields to the record. It goes no
    deeper; links on the opened page are not followed. Requests to the same host are queued by throttle; if
    record pages are on another site, the rule's delay is applied to that site too."""
    say = progress or (lambda m: None)
    todo = res.items  # no limit: every record from the list
    first = next((it.get(detail.link_field) for it in todo
                  if isinstance(it.get(detail.link_field), str) and it[detail.link_field].startswith("http")), None)
    if first:
        res.detail_sec_per_page = await detail_sec_per_page(detail, first, delay_ms)
        say(f"{len(todo)} record pages will be opened, estimated time {fmt_duration(len(todo) * res.detail_sec_per_page)}")
    allowed: dict[str, bool] = {}
    names = [f.name for f in detail.fields]
    page = None
    own_session = None
    client = None
    try:
        if detail.fetch == "http":
            client = B.http_client()
        else:
            if session is None:
                own_session = B.BrowserSession()
                session = await own_session.__aenter__()
            page = await session.new_page()
        for i, item in enumerate(todo, 1):
            link = item.get(detail.link_field)
            if isinstance(link, list):
                link = link[0] if link else None
            if not isinstance(link, str) or not link.startswith("http"):
                continue
            host = urlparse(link).netloc
            if host not in allowed:
                B.set_host_delay(link, delay_ms)  # be polite to record pages on other sites too
                allowed[host] = (not respect_robots) or await B.robots_allowed(link)
                if not allowed[host]:
                    res.errors.append(f"robots.txt disallows record pages on {host}; skipped")
            if not allowed[host]:
                continue
            try:
                if client is not None:
                    html, final = await B.http_get(client, link)
                else:
                    await B.goto(page, link, settle_ms=800)
                    if detail.wait_for:
                        try:
                            await page.locator(B.pw_selector(detail.wait_for)).first.wait_for(timeout=8000)
                        except Exception:
                            pass
                    html, final = await page.content(), page.url
            except Exception as e:
                res.detail_failed += 1  # messages are capped at 50, the counter is not
                if len(res.errors) < 50:
                    res.errors.append(f"could not open record page {link}: {_err(e)}")
                continue
            row, errs = extract_page(html, final, detail.fields)
            for e in errs:
                if e not in res.errors:
                    res.errors.append(e)
            for n in names:  # detail keys exist only on records whose page opened (fill rates are measured on these)
                item.setdefault(n, None)
            for k, v in row.items():
                if v not in (None, []) or item.get(k) in (None, []):
                    item[k] = v
            res.detail_visited += 1
            if i % 10 == 0 or i == len(todo):
                say(f"record page {i}/{len(todo)}")
    finally:
        if client is not None:
            await client.aclose()
        if page is not None:
            await page.close()
        if own_session is not None:
            await own_session.__aexit__(None, None, None)


def needs_browser(rule: Rule, force_fetch: Optional[str] = None) -> bool:
    return ((force_fetch or rule.fetch) == "browser" or rule.pagination.type in ("infinite_scroll", "load_more")
            or (rule.search is not None and rule.search.how == "form"))


# ---------------------------------------------------------------- search

async def search_in_page(page, s: Search, query: str) -> None:
    """On the search page, type the term into the box (or pick it from the dropdown) and submit."""
    loc = page.locator(B.pw_selector(s.input)).first
    await loc.wait_for(state="attached", timeout=10000)
    if s.is_select:
        try:
            await loc.select_option(value=query, timeout=5000)
        except Exception:
            await loc.select_option(label=query, timeout=5000)
    else:
        try:
            await loc.click(timeout=2500)  # some search boxes open only when clicked
        except Exception:
            pass
        try:
            await loc.fill(query, timeout=4000)
        except Exception:  # hidden/collapsed box: set the value directly
            await loc.evaluate("(e, q) => { e.value = q; e.dispatchEvent(new Event('input', {bubbles: true}));"
                               " e.dispatchEvent(new Event('change', {bubbles: true})); }", query)
    if s.is_select and s.submit is None:
        before = page.url
        await page.wait_for_timeout(s.wait_ms)  # lists that refresh by themselves on selection
        if page.url != before:
            return
        try:  # page did not change: submit the form (the submit button may not have had a unique selector)
            sent = await loc.evaluate("e => { if (!e.form) return false; e.form.requestSubmit ? "
                                      "e.form.requestSubmit() : e.form.submit(); return true; }")
        except Exception:
            sent = True  # the submit changed the page (execution context destroyed)
        if sent:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            await page.wait_for_timeout(s.wait_ms)
        return
    if s.submit is not None:
        try:
            await page.locator(B.pw_selector(s.submit)).first.click(timeout=5000)
        except Exception:
            await loc.evaluate("e => e.form && (e.form.requestSubmit ? e.form.requestSubmit() : e.form.submit())")
    else:
        try:
            await loc.press("Enter", timeout=4000)
        except Exception:
            await loc.evaluate("e => e.form && (e.form.requestSubmit ? e.form.requestSubmit() : e.form.submit())")
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
        await page.wait_for_load_state("load", timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(s.wait_ms)


async def read_options(s: Search, session: Optional[B.BrowserSession]) -> tuple[list[str], list[str]]:
    """Current options of the search form's dropdown (values, labels). Empty/placeholder options are skipped."""
    if not s.page_url or s.input is None:
        return [], []
    html = None
    try:
        async with B.http_client() as c:
            html, _ = await B.http_get(c, s.page_url)
    except Exception:
        html = None
    if html is None and session is not None:
        page = await session.new_page()
        try:
            await B.goto(page, s.page_url, settle_ms=1000)
            html = await page.content()
        finally:
            await page.close()
    if not html:
        return [], []
    sel = Selector(text=html)
    try:
        nodes = sel.xpath(s.input.value) if s.input.kind == "xpath" else sel.css(s.input.value)
    except Exception:
        return [], []
    vals, labels = [], []
    for o in (nodes[0].xpath(".//option") if nodes else []):
        label = re.sub(r"\s+", " ", " ".join(o.xpath(".//text()").getall())).strip()
        v = o.attrib.get("value", label).strip()
        if "disabled" in o.attrib or is_placeholder_option(v, label):
            continue
        if v not in vals:
            vals.append(v)
            labels.append(label)
    return vals, labels


_TRACK_PARAM = re.compile(r"^(s_token|s_time|utm_\w+|srsltid|_ga)$", re.I)


def clean_result_url(v: str, query: str, param: Optional[str] = None) -> str:
    """Remove the search's traces from a record link: the search parameter (named `param`) and known tracking
    parameters (s_token, utm_*), so the same record has the same link across queries and is deduplicated. Other
    parameters are not dropped just because their value equals the query (id=1 with query '1')."""
    p = urlparse(v)
    if not p.query:
        return v
    keep = [(k, x) for k, x in parse_qsl(p.query, keep_blank_values=True)
            if not _TRACK_PARAM.match(k) and not (param and k == param and x.strip().lower() == (query or "").lower())]
    if len(keep) == len(parse_qsl(p.query, keep_blank_values=True)):
        return v  # no change: leave the encoding untouched
    return urlunparse(p._replace(query=urlencode(keep)))


def _search_param(s: Search) -> Optional[str]:
    """Name of the query parameter that carries the term in url_template (filter_name, q …)."""
    if s.how != "url" or not s.url_template:
        return None
    return next((k for k, v in parse_qsl(urlparse(s.url_template).query, keep_blank_values=True)
                 if v == "{query}"), None)


# Stop reasons meaning no result page of the query could be read
SEARCH_FAIL = {"search_error", "goto_error", "http_error", "error"}


def _saturated(sub: RunResult, cap: Optional[int]) -> bool:
    """The query may match more than the site can show: pages did not run out, or the result cap was hit."""
    return sub.stop_reason == "max_pages" or (cap is not None and len(sub.items) >= cap)


async def _run_search(rule: Rule, url: str, session, max_pages, progress, force_fetch, respect_robots,
                      queries, max_queries) -> RunResult:
    """Reads the result page of each query with the rule's list part and merges the records (unique_by).
    The query that first found a record is written as `_query`. In prefix enumeration, a prefix that hits the
    cap is extended."""
    from collections import Counter, deque
    s = rule.search
    say = progress or (lambda m: None)
    res = RunResult(url=url, fetch_used="browser" if needs_browser(rule, force_fetch) else (force_fetch or rule.fetch))
    col = _Collector(rule)
    labels: dict[str, str] = {}
    en = s.enum
    prefix = False
    by_label: dict[str, str] = {}
    if s.is_select and s.option_values:  # in a dropdown the user types the visible label; the value is what gets searched
        labels = {v: l for v, l in zip(s.option_values, s.option_labels) if l and l != v}
        by_label = {l.lower(): v for v, l in zip(s.option_values, s.option_labels)}
    if queries:
        terms = list(dict.fromkeys(by_label.get(t.lower(), t) for t in queries))
    elif s.terms == "given" or en is None:
        terms = list(s.queries)
    elif en.method == "empty":
        terms = [""]
    elif en.method == "wildcard":
        terms = [en.wildcard]
    elif en.method == "options":
        vals, labs = await read_options(s, session)
        if not vals:
            vals, labs = list(en.options), list(en.labels)
            if vals:
                res.errors.append("could not read the search options from the page; using the list from learning")
        terms = vals
        labels.update({v: l for v, l in zip(vals, labs) if l and l != v})
    else:  # prefix
        import itertools
        prefix = True
        n = max(1, en.min_chars)
        terms = ["".join(t) for t in itertools.product(en.alphabet, repeat=n)]
    if not terms:
        res.errors.append("no terms to search: give terms when running (--query) or edit the rule's search terms")
        res.stop_reason = "no_terms"
        return res
    limit = s.max_queries if max_queries is None else max_queries
    say(f"search: {len(terms)} queries" + (" (prefixes that hit the cap are extended)" if prefix else "")
        + (f", query limit {limit}" if limit else ""))
    if s.how == "url" and respect_robots and not await B.robots_allowed(fill_template(s.url_template, terms[0] or "a")):
        res.errors.append(f"robots.txt disallows the search result pages: {fill_template(s.url_template, terms[0])}")
        res.stop_reason = "robots"
        return res
    cap = en.result_cap if en else None
    sparam = _search_param(s)
    queue, done = deque(terms), set()
    counts: Counter = Counter()
    natural_counts: dict[str, int] = {}
    expanded: set[str] = set()
    unresolved = []

    def expand(q: str) -> bool:
        if not prefix or q in expanded or len(q) >= en.max_depth:
            return False
        expanded.add(q)
        queue.extend(q + c for c in en.alphabet)
        return True

    own = None
    if session is None and needs_browser(rule, force_fetch):
        own = B.BrowserSession()
        session = await own.__aenter__()
    try:
        while queue:
            q = queue.popleft()
            if q in done:
                continue
            if limit and len(done) >= limit:
                res.queries_left = len(set(queue) - done) + 1
                res.stop_reason = "query_limit"
                break
            done.add(q)
            target = fill_template(s.url_template, q) if s.how == "url" else s.page_url
            sub = await _run_list(rule, target, session, max_pages, None, force_fetch,
                                  query=q if s.how == "form" else None)
            if sub.stop_reason == "http_error" and not sub.items and any(
                    re.match(r"HTTP (404|410) ", e) for e in sub.errors):  # some sites return 404 for a search with no results
                sub.errors, sub.stop_reason = [], "empty_page"
            failed = sub.stop_reason in SEARCH_FAIL and not sub.items
            if failed:
                res.search_failed += 1
            for it in sub.items:  # only the site's own record links (external links are left alone)
                for f in rule.fields:
                    v = it.get(f.name)
                    if "absolute_url" in f.transforms and isinstance(v, str) and v.startswith("http") \
                            and (urlparse(v).hostname or "").removeprefix("www.") == rule.site:
                        it[f.name] = clean_result_url(v, q, sparam)
            new = 0
            for it in sub.items:
                k = col.key(it)
                if k not in col.keys:
                    col.keys.add(k)
                    it["_query"] = labels.get(q, q)
                    col.items.append(it)
                    new += 1
            for p in sub.pages:
                p.page, p.query = len(res.pages) + 1, q
                res.pages.append(p)
            res.errors.extend(e for e in sub.errors if e not in res.errors and len(res.errors) < 50)
            res.rounds += sub.rounds
            res.delay_ms = max(res.delay_ms, sub.delay_ms)
            res.queries.append({"query": labels.get(q, q), "items": len(sub.items), "new": new,
                                "pages": len(sub.pages), "stop_reason": sub.stop_reason,
                                **({"failed": True} if failed else {})})
            say(f"search {len(done)} '{labels.get(q, q)}': {len(sub.items)} results, {new} new (total {len(col.items)})"
                + (f" [{sub.stop_reason}]" if not sub.natural_end else ""))
            if prefix:
                if sub.natural_end and len(sub.items) >= 10 and cap is None:
                    counts[len(sub.items)] += 1
                    natural_counts[q] = len(sub.items)
                    top, n = counts.most_common(1)[0]
                    if n >= 3:  # many prefixes gave the same number of results: the site cuts results at this number
                        cap = top
                        say(f"result cap detected: at most {cap} results per query; extending prefixes at the cap")
                        for pq, c in natural_counts.items():
                            if c >= cap and not expand(pq) and pq not in expanded and pq not in unresolved:
                                unresolved.append(pq)  # cap hit at the longest prefix: there may be more results
                if _saturated(sub, cap):
                    expand(q)
                    if q not in expanded:  # longest prefix reached: there may be more results
                        unresolved.append(q)
            elif not failed and not sub.natural_end and sub.stop_reason != "form_no_pagination":
                unresolved.append(q)  # page limit, or stopped midway (click error etc.)
    finally:
        if own is not None:
            await own.__aexit__(None, None, None)
    res.items = col.items
    if not res.stop_reason:
        if res.queries and res.search_failed > 0.2 * len(res.queries):
            res.stop_reason = "search_errors"
        else:
            res.stop_reason = "max_pages" if unresolved else "queries_done"
    if unresolved:
        say(f"{len(unresolved)} queries hit a limit, there may be more results: "
            + ", ".join(repr(labels.get(q, q)) for q in unresolved[:8]))
    return res


async def _run_list(rule: Rule, url: str, session: Optional[B.BrowserSession],
                    max_pages: Optional[int], progress: Progress, force_fetch: Optional[str],
                    query: Optional[str] = None) -> RunResult:
    """query: for rules that search via a form, the term typed into the box on the first page (needs a browser)."""
    fetch = force_fetch or rule.fetch
    if rule.pagination.type in ("infinite_scroll", "load_more") or query is not None:
        fetch = "browser"
    mp = max_pages if max_pages is not None else rule.pagination.max_pages
    limit = UNLIMITED if mp == ALL_PAGES else max(1, mp)  # 0 = all: until the pages run out naturally
    res = RunResult(url=url, fetch_used=fetch)
    say = progress or (lambda m: None)
    crawl = await B.robots_delay_ms(url)
    res.delay_ms = max(rule.delay_ms, crawl)
    B.set_host_delay(url, res.delay_ms)  # page loads/HTTP requests are queued with this spacing
    if crawl > rule.delay_ms and limit > 1:
        say(f"robots.txt Crawl-delay: waiting {res.delay_ms / 1000:.0f} s between pages")
    col = _Collector(rule)
    try:
        if fetch == "http":
            await _run_http(rule, url, limit, res, col, say)
        elif session is None:
            async with B.BrowserSession() as s:
                await _run_browser(rule, url, s, limit, res, col, say, query)
        else:
            await _run_browser(rule, url, session, limit, res, col, say, query)
    except Exception as e:  # an unexpected error on one page must not crash the whole run
        res.errors.append(f"run error: {type(e).__name__}: {(str(e).splitlines() or [''])[0][:200]}")
        res.stop_reason = res.stop_reason or "error"
    res.items = col.items
    return res


def _record(res: RunResult, col: _Collector, html: str, page_url: str, rule: Rule) -> PageStat:
    ex = extract_html(html, page_url, rule)
    for e in ex.errors:
        if e not in res.errors:
            res.errors.append(e)
    st = PageStat(page=len(res.pages) + 1, url=page_url, matched=ex.matched, new=col.add(ex.items), rows=len(ex.items))
    res.pages.append(st)
    return st


def _page_msg(st: PageStat, col: _Collector) -> str:
    return f"page {st.page}: {st.matched} matches, {st.new} new (total {len(col.items)})"


def _err(e: Exception) -> str:
    return f"{type(e).__name__}: {(str(e).splitlines() or [''])[0][:160]}"


# ---------------------------------------------------------------- http

async def _run_http(rule, url, limit, res, col, say):
    pg = rule.pagination
    base = url if pg.type == "url_param" else None
    visited: set[str] = set()
    zero_streak = 0
    async with B.http_client() as client:
        cur = url
        for i in range(limit):
            if base is not None:
                cur = page_url(base, rule, i)
            try:
                html, final = await B.http_get(client, cur)
            except httpx.HTTPStatusError as e:
                if i > 0 and e.response.status_code in (404, 410):
                    res.stop_reason = "empty_page"  # past the last page
                else:
                    res.errors.append(f"HTTP {e.response.status_code} error: {cur}")
                    res.stop_reason = "http_error"
                return
            except Exception as e:
                res.errors.append(f"HTTP error {cur}: {_err(e)}")
                res.stop_reason = "http_error"
                return
            if base is not None and i == 0 and final != cur:
                base = final  # if redirected, build the next pages from the target URL
            if base is not None and i > 0 and (not _param_kept(final, rule) or final in visited):
                res.errors.append(f"page {i + 1} was redirected and the page parameter was dropped: {final}")
                res.stop_reason = "redirected"
                return
            visited.update({cur, final})
            res.rounds = i + 1
            st = _record(res, col, html, final, rule)
            say(_page_msg(st, col))
            if pg.type == "none":
                res.stop_reason = "single_page"
                return
            if i > 0 and st.matched == 0:
                res.stop_reason = "empty_page"
                return
            if i > 0 and st.new == 0:
                zero_streak += 1
                if zero_streak >= 2 or pg.type != "url_param":
                    res.stop_reason = "no_new_items"
                    return
            else:
                zero_streak = 0
            if pg.type == "next_button":
                nxt = next_href(html, final, rule, visited)
                if not nxt:
                    res.stop_reason = "no_next_link"
                    return
                if i == limit - 1:
                    break
                cur = nxt
            elif pg.type != "url_param":
                res.stop_reason = "http_unsupported"
                return
        res.stop_reason = "max_pages"


# ---------------------------------------------------------------- browser

async def _prepare(page, rule, url, res, first: bool, query: Optional[str] = None) -> bool:
    """Open the page, apply the setup steps, (search for the term if it is a form search), wait for the items.
    Returns whether the items appeared."""
    await B.goto(page, url)
    errs = await B.run_setup(page, rule.setup)
    res.errors.extend(e for e in errs if e not in res.errors)
    if query is not None and rule.search is not None:
        try:
            await search_in_page(page, rule.search, query)
        except Exception as e:
            res.errors.append(f"search failed ('{query}'): {_err(e)}")
            res.stop_reason = "search_error"  # do not read a page where the search did not happen
            return False
    if rule.wait_for:
        try:
            await page.locator(B.pw_selector(rule.wait_for)).first.wait_for(
                state="attached", timeout=15000 if first else 8000)
        except Exception:
            if first:  # not finding it on later pages usually means "no more pages"
                msg = f"wait_for selector not found: {rule.wait_for.value}"
                if msg not in res.errors:
                    res.errors.append(msg)
            return False
    return True


async def _fingerprint(page, rule) -> str:
    try:
        loc = page.locator(B.pw_selector(rule.item))
        n = await loc.count()
        if n == 0:
            return "0"
        return f"{n}|{(await loc.first.inner_text(timeout=2000))[:200]}|{(await loc.last.inner_text(timeout=2000))[:200]}"
    except Exception:
        return ""


async def _wait_change(page, rule, before: str, timeout_ms: int) -> bool:
    """After a click/scroll, wait until the item list changes."""
    waited, step = 0, 250
    while waited < timeout_ms:
        await page.wait_for_timeout(step)
        waited += step
        if await _fingerprint(page, rule) not in (before, ""):
            await page.wait_for_timeout(300)  # short margin for late-arriving parts
            return True
    return False


async def _clickable(loc) -> bool:
    try:
        if await loc.count() == 0 or not await loc.is_visible():
            return False
        if await loc.get_attribute("disabled") is not None:
            return False
        if (await loc.get_attribute("aria-disabled") or "") == "true":
            return False
        cls = (await loc.get_attribute("class") or "").lower().split()
        if {"disabled", "passive", "inactive", "is-disabled"} & set(cls):  # bkmkitap last page: 'next passive'
            return False
        return await loc.evaluate("e => getComputedStyle(e).pointerEvents !== 'none'")
    except Exception:
        return False


async def _run_browser(rule, url, session, limit, res, col, say, query=None):
    pg = rule.pagination
    page = await session.new_page()
    try:
        if pg.type == "url_param":
            zero_streak = 0
            base = url
            seen_urls: set[str] = set()
            for i in range(limit):
                cur = page_url(base, rule, i)
                if i == 1 and query is not None and page.url.split("#")[0] == url.split("#")[0]:
                    # the form search did not change the URL: ?page=2 would open the unsearched list, so no pagination
                    res.errors.append("the form search does not change the URL; later result pages cannot be opened by URL")
                    res.stop_reason = "form_no_pagination"
                    return
                try:
                    ok = await _prepare(page, rule, cur, res, first=i == 0, query=query if i == 0 else None)
                except Exception as e:
                    res.errors.append(f"could not open page {cur}: {_err(e)}")
                    res.stop_reason = "goto_error"
                    return
                if res.stop_reason == "search_error":
                    return
                if i == 0 and (page.url != cur or query is not None):
                    base = page.url
                if i > 0 and (not _param_kept(page.url, rule) or page.url in seen_urls):
                    res.errors.append(f"page {i + 1} was redirected and the page parameter was dropped: {page.url}")
                    res.stop_reason = "redirected"
                    return
                seen_urls.add(page.url)
                # no scrolling for url_param: some sites append the next page when scrolled
                res.rounds = i + 1
                st = _record(res, col, await page.content(), page.url, rule)
                say(_page_msg(st, col))
                if i > 0 and (not ok or st.matched == 0):
                    res.stop_reason = "empty_page"
                    return
                if i > 0 and st.new == 0:
                    zero_streak += 1
                    if zero_streak >= 2:
                        res.stop_reason = "no_new_items"
                        return
                else:
                    zero_streak = 0
            res.stop_reason = "max_pages"
            return

        try:
            await _prepare(page, rule, url, res, first=True, query=query)
        except Exception as e:
            res.errors.append(f"could not open page {url}: {_err(e)}")
            res.stop_reason = "goto_error"
            return
        if res.stop_reason == "search_error":
            return

        if pg.type == "none":
            await B.gentle_scroll(page, 2)
            res.rounds = 1
            _record(res, col, await page.content(), page.url, rule)
            res.stop_reason = "single_page"
            return

        if pg.type == "next_button":
            visited = {url}
            for i in range(limit):
                html = await page.content()
                visited.add(page.url)
                res.rounds = i + 1
                st = _record(res, col, html, page.url, rule)
                say(_page_msg(st, col))
                if st.new == 0 and i > 0:
                    res.stop_reason = "no_new_items"
                    return
                nxt = next_href(html, page.url, rule, visited)
                loc = page.locator(B.pw_selector(pg.selector)).first
                if not nxt and not await _clickable(loc):
                    res.stop_reason = "no_next_button"
                    return
                if i == limit - 1:
                    break
                if nxt:
                    visited.add(nxt)
                    try:
                        ok = await _prepare(page, rule, nxt, res, first=False)
                    except Exception as e:
                        res.errors.append(f"could not open next page {nxt}: {_err(e)}")
                        res.stop_reason = "goto_error"
                        return
                    if not ok:
                        res.stop_reason = "empty_page"
                        return
                    continue
                await B.sleep_ms(res.delay_ms)
                before = await _fingerprint(page, rule)
                try:
                    await loc.click(timeout=8000)
                except Exception as e:
                    res.stop_reason = f"click_error: {type(e).__name__}"
                    return
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                if not await _wait_change(page, rule, before, max(pg.wait_ms, 10000)):
                    res.stop_reason = "no_new_items"
                    return
            res.stop_reason = "max_pages"
            return

        # load_more / infinite_scroll: take a snapshot every round (virtual lists may remove old items)
        st = _record(res, col, await page.content(), page.url, rule)
        say(f"initial load: {st.matched} matches")
        stale = 0
        for i in range(limit):
            before = await _fingerprint(page, rule)
            wait = pg.wait_ms if stale == 0 else pg.wait_ms * 2
            if pg.type == "load_more":
                loc = page.locator(B.pw_selector(pg.selector)).first
                if not await _clickable(loc):
                    res.stop_reason = "no_load_more_button"
                    break
                try:
                    await loc.scroll_into_view_if_needed(timeout=5000)
                    await loc.click(timeout=8000)
                except Exception as e:
                    res.stop_reason = f"click_error: {type(e).__name__}"
                    break
                await _wait_change(page, rule, before, wait)
            else:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                if not await _wait_change(page, rule, before, wait):
                    await page.mouse.wheel(0, 3000)  # some sites expect a real wheel event
                    await _wait_change(page, rule, before, wait)
            res.rounds = i + 1
            st = _record(res, col, await page.content(), page.url, rule)
            say(f"round {i + 1}: {st.matched} items on page, {st.new} new (total {len(col.items)})")
            if st.new == 0:  # progress = new unique records (changing texts like clocks/badges do not count)
                stale += 1
                if stale >= 2:
                    res.stop_reason = "nothing_loaded"
                    break
            else:
                stale = 0
            if res.delay_ms > 1000:
                await B.sleep_ms(res.delay_ms)
        else:
            res.stop_reason = "max_pages"
    finally:
        await page.close()
