"""Trial and error with the LLM: propose a rule -> run it on the live page -> check -> fix -> fix it in place."""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse
from dataclasses import dataclass, field
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from . import browser as B
from .executor import RunResult, detail_sec_per_page, enrich, fmt_duration, run_rule, search_in_page
from .llm import DEFAULT_MODEL, ESCALATION_MODEL, LLMError, ask_json
from .extract import parse_number
from .rules import (Detail, DetailDraft, Enumeration, Rule, RuleDraft, Search, Sel, choose_url_pattern,
                    default_url_pattern, dumps, fill_template, is_placeholder_option, now_iso, site_of, slug,
                    term_from_url)
from .simplify import COUNT_RE, MARK_HIDDEN_JS, SearchField, SimplePage, clean_tree, find_regions, simplify
from .validate import check, fill_rates, report


class Review(BaseModel):
    verdict: Literal["accept", "revise"]
    reason: str = Field(description="Short reason")
    rule: Optional[RuleDraft] = Field(default=None, description="if verdict=revise: the corrected FULL rule")


DRAFT_SCHEMA = RuleDraft.model_json_schema()
REVIEW_SCHEMA = Review.model_json_schema()

GOAL_GUIDE = """USER REQUEST: build the rule around what the user wrote.
- records_asked: the kind of record the user wants, in their words; the item selector selects those
  records on THIS page. A differently named or broader/narrower version of the same thing counts as a
  match (product/book, news/post, listing/product). Set records_match=false only when this page clearly lists
  something else and say so in goal_fields_note.
- in_linked_pages: if the context makes it clear that the requested data is inside each record's own
  linked page (user wants videos and the page lists albums of videos; a product attribute that list
  cards do not show but a product page would), set in_linked_pages=true, select those parent records,
  include their own link field, and leave the inner data field=null. A separate step opens each record's
  page (one level only). Do not use it as a guess when the context does not suggest it.
- requested: one entry per thing the user named ("link, name, price" = 3 entries), each mapped to the
  field on THIS page that holds it. If something is not on this page, field=null — do not invent a
  selector for it. Extra fields you add yourself stay optional.
- This rule only reads this page and its pagination.
- search_needed: if SEARCH FIELD CANDIDATES are listed and (a) the user wants records matching a specific
  name/title/term (e.g. "videos titled cat", "the profile of the person named X"), or (b) this page does not
  list the requested records but offers a search box / search form for them, set search_needed=true,
  search_field = the candidate number to use (the main site search box, or a select whose options are
  the record groups), and search_terms = the exact term(s) from the user's words. search_terms=[] when the
  user wants ALL such records without naming one (the system then generates queries itself). With
  search_needed=true the rest of the rule is ignored for now (a next step writes it on the result page),
  so keep item/fields minimal. If the page already lists exactly the requested records, search_needed=false:
  a term that is just this page's own category or filter (goal "novels" on the novels category
  page) does not need search. Pick a search field that searches the requested kind of record (not a
  login, comment or newsletter form; not a site-wide search for a different kind of thing if a more
  specific one exists).
"""

DETAIL_GUIDE = """You are looking at the OWN PAGE of one record from a list (one level deep). The list rule
already extracts the record fields shown below. Write fields that read the MISSING requested data from
this page.
- found=false if this page does not contain that data either (then fields may be empty).
- requested: each missing item (user's words) mapped to a field below, or null if absent.
- fields: selectors relative to the whole page (css, or xpath starting with //). For repeated values
  (every video/file/image link, all specs) use multiple=true with a selector matching each one. For
  links use attr href/src with transforms ["strip","absolute_url"]. Field names must be new (not used by
  the list rule), short, lowercase ASCII.
- Read only this page: do not plan to follow its links further.
"""

GUIDE = """RULES FOR WRITING THE RULE
- item: selector matching EACH repeated record the user wants (one match per record). Prefer stable,
  meaningful class names / ids / data-* attributes. Avoid auto-generated hash classes and positional
  nth-child selectors. The region candidates list real match counts measured on the page — use them.
  A region's "sample html" is the full raw card, including parts hidden in the current view (e.g. a list
  view block); the rule reads hidden parts too, so fields may come from there.
- fields: selectors are RELATIVE to the item (xpath must start with '.'). Omit selector to use the item
  element itself (e.g. when the item IS the <a>). For links use attr "href" with transforms
  ["strip","absolute_url"]. Use attr "text" for visible text, or any attribute name. Use transform
  "number" for prices/counts (applied to the final text; use a selector that targets only the price).
  Use "strip_quotes" when text is wrapped in decorative quotes. Every single-value field the user explicitly
  asked for (e.g. link, name/title, price) must be required=true; list fields (multiple=true) that can be
  legitimately empty, and extra fields you add yourself, stay optional.
- Names/titles: sites often shorten visible text ("Prestigio SmartB..."). If the element has a title,
  aria-label or similar attribute holding the full text, use that attribute instead of "text".
- setup: only when needed. If a cookie/consent overlay blocks the page, click its "reject" / "only
  necessary" / close button (prefer the most privacy-preserving option), optional=true.
- wait_for: a selector that exists once the records are loaded (often the item selector).
- pagination (look at the BUTTON/LINK candidates and pager hrefs):
  * url_param: page links differ only by a query param (?page=2, ?pi=2, ?p=2, ?offset=20) — most
    reliable. Set param, page2_value = the value in the site's own link to page 2, and step.
  * next_button: a "next" link/button; its selector should match exactly ONE element (see the match
    counts; use the xpath text form or an attribute if the css matches several pager links).
  * load_more: a "show more / load more" button that appends records.
  * infinite_scroll: records append while scrolling, no buttons.
  If a SCROLL OBSERVATION line shows page-like requests made while scrolling (e.g. ...?pg=2), the site
  loads pages by that parameter: prefer url_param with that exact param name and page-2 value — it is
  more reliable than scrolling. The DOM shown is the page BEFORE scrolling.
  * none: everything is on this one page.
  Set max_pages high enough to reach min_total.
- expect: YOU choose validation criteria from what you see:
  * min_items_per_page: about the number of records on the first page before any scrolling (a bit
    lower to be safe).
  * min_total: if a total count hint exists (e.g. "1,234 products") use ~90% of the part reachable with
    max_pages; otherwise items_per_page x pages you expect. Use 1-page count when pagination is none.
  * unique_by: a field that identifies a record (usually the url field).
  * field_patterns: regex sanity checks on the FINAL value (after transforms; lists are checked per
    element), e.g. {"url": "^https?://"}.
- url_pattern: a Python regex matching the URLs of this site that share this layout (e.g. all category
  pages, not just this one), and it must match the given URL(s).
- fetch: "browser". Keep field names short, lowercase, ASCII (url, title, price ...).
"""


def initial_prompt(goal: str, sp: SimplePage) -> str:
    return (f"Write a site-specific scraping rule for the page below.\n\n{_goal_block(goal)}\n{GOAL_GUIDE}\n"
            f"{GUIDE}\nThe rule must also work on other pages of the same site with the same layout.\n\n"
            f"=== PAGE ===\n{sp.as_prompt()}\n\n{_goal_block(goal)}")


def _goal_block(goal: str) -> str:
    return f"USER REQUEST (verbatim, may be in any language): <<<{goal.strip()}>>>\n"


def _requested_view(rule: Rule, rep: dict) -> str:
    """What each requested item maps to, with sample values: the review compares this to the request."""
    # report() keys: accept both the English and the older Turkish names
    samples = rep.get("samples", rep.get("ornekler", []))[:3]
    fills = rep.get("fill_rates", rep.get("doluluk", {}))
    lines = [f"- records asked: '{rule.records_asked}' -> item selector {rule.item.value!r} "
             f"(records_match={rule.records_match})"]
    for r in rule.requested:
        vals = [s.get(r.field) for s in samples] if r.field else []
        lines.append(f"- '{r.asked}' -> field {r.field!r}, fill {fills.get(r.field, 0)}, "
                     f"samples: {json.dumps(vals, ensure_ascii=False)[:300]}")
    return "\n".join(lines)


def review_prompt(goal: str, rule: Rule, rep: dict, issues: list[str], sp: SimplePage, note: str = "") -> str:
    draft = rule.model_dump(include=set(RuleDraft.model_fields), exclude_none=True)
    return (f"{_goal_block(goal)}\nYou wrote this rule:\n{json.dumps(draft, ensure_ascii=False)}\n\n"
            f"It was executed on the LIVE page (test limited to a few pages). Results:\n{dumps(rep)}\n\n"
            f"AUTOMATIC CHECK PROBLEMS:\n" + ("\n".join(f"- {i}" for i in issues) if issues else "- none") + "\n\n"
            + (f"NOTE: {note}\n\n" if note else "") +
            f"WHAT THE USER ASKED FOR vs WHAT THE RULE DELIVERS:\n{_requested_view(rule, rep)}\n\n"
            "Decide. Check that each mapped field's sample values really are what the user asked for (a price "
            "field holds prices, a link field the record's own link). For each requested item with field=null, "
            "look once in the record text / sample html of the item region on THIS page (author, brand, date "
            "etc. often appear as a plain line inside the card): if it is there, revise and add that field; if "
            "it is really not there (or in_linked_pages=true: a later step reads it from each record's own "
            "page), field=null is fine and is not a reason to revise. "
            "verdict=accept ONLY if the extracted records really are what the user asked for (right "
            "records, not menus/ads/filters/related items; values look correct; pagination brings new records) "
            "AND there are no automatic check problems. Also check the visited page URLs: page 2 must be the "
            "site's own page-2 link and no page may be skipped or repeated. Otherwise verdict=revise and return the corrected FULL "
            "rule. Only change expectations if they were clearly wrong, and say why in reason.\n\n"
            f"{GOAL_GUIDE}\n{GUIDE}\n=== PAGE ===\n{sp.as_prompt(max_regions=6)}\n\n{_goal_block(goal)}")


@dataclass
class Attempt:
    rule: Rule
    issues: list[str]
    total: int
    report: dict
    run: object = None


@dataclass
class LearnResult:
    rule: Optional[Rule]
    accepted: bool
    attempts: list[Attempt] = field(default_factory=list)
    cost_usd: float = 0.0
    llm_calls: int = 0
    failure: str = ""  # why the rule was not saved (e.g. the requested data is not on the page)


_PAGEISH = re.compile(r"[?&](page|pg|p|pi|sayfa|sf|offset|start|skip|from|cursor|after)=", re.I)


async def snapshot_page(session: B.BrowserSession, url: str, action=None) -> SimplePage:
    """Simplify the page as first loaded; add what changes on scrolling (item counts, background requests)
    as hints. Sites with no page link in the DOM (fetching ?pg=2 via XHR) are detected this way.
    action: an operation to apply after the page opens (e.g. a search through a form)."""
    page = await session.new_page()
    requests: list[str] = []

    def on_request(req):
        if req.resource_type in ("xhr", "fetch", "document") and _PAGEISH.search(req.url) \
                and req.url not in requests:
            requests.append(req.url)

    try:
        await B.goto(page, url, settle_ms=2500)
        if action is not None:
            await action(page)
        await page.evaluate(MARK_HIDDEN_JS)
        html0 = await page.content()
        page.on("request", on_request)
        await B.gentle_scroll(page, 4)
        await page.wait_for_timeout(1500)
        html1 = await page.content()
        sp = simplify(html0, page.url)
        if not sp.regions:  # if content only appears after scrolling, use the scrolled version
            sp = simplify(html1, page.url)
        grown = []
        for r in sp.regions[:5]:
            after = _count_css(html1, r.short_selector)
            if after > r.short_count > 0:
                grown.append(f"{r.short_selector}: {r.short_count} → {after}")
        if grown:
            sp.scroll_hints.append("Item count grew when the page was scrolled down (the site loads on scroll): "
                                   + "; ".join(grown))
        if requests:
            sp.scroll_hints.append("Page-like requests the site made while scrolling (the url_param "
                                   "parameter name and value can be read from these): " + " | ".join(requests[:6]))
        return sp
    finally:
        await page.close()


def _count_css(html: str, sel: str) -> int:
    try:
        import lxml.html
        return len(lxml.html.document_fromstring(html).cssselect(sel))
    except Exception:
        return 0


def _to_rule(draft: RuleDraft, *, name: str, url: str, goal: str, also: list[str]) -> Rule:
    d = draft.model_dump()
    d["url_pattern"] = choose_url_pattern(d.get("url_pattern"), url, list(also))
    return Rule(**d, name=name, site=site_of(url), goal=goal)


def _short(text, n: int = 300) -> str:
    text = str(text or "")
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


# ---------------------------------------------------------------- search

NONSENSE = "qzxjvkw"  # distinctive term that gives no results: used to find its place in the search URL
ALL_PROBES = ["", "*", "a", "e", "an", "ar", "ara"]  # tried in order when 'all' records are wanted
FEW_RESULTS = 8  # with fewer results for the term, pagination is not visible: learn on a broad query


@dataclass
class SearchSetup:
    search: Search
    probe: str                      # query that produces the result page used for learning
    sp: SimplePage                  # simplified version of that result page
    terms: list[str]                # the user's terms (empty means 'all')
    extra: list[str] = field(default_factory=list)  # extra queries the rule is also tested with
    method: Optional[str] = None    # for 'all': empty | wildcard | options | prefix


def _template_from(url_after: str, token: str, name: str = "") -> Optional[str]:
    """Replace the term's place in the post-search URL with {query} (query parameter, path segment or #).
    If several parameters carry the same value (select value '3' and page=3), the one named like the field
    is chosen; otherwise only the first is replaced."""
    from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse
    p = urlparse(url_after)
    q = parse_qsl(p.query, keep_blank_values=True)
    hits = [i for i, (_, v) in enumerate(q) if v == token]
    if hits:
        pick = next((i for i in hits if name and q[i][0] == name), hits[0])
        pairs = [(k, "__KQ__" if i == pick else v) for i, (k, v) in enumerate(q)]
        return urlunparse(p._replace(query=urlencode(pairs))).replace("__KQ__", "{query}")
    segs = p.path.split("/")
    if quote(token) in segs:
        i = segs.index(quote(token))
        segs[i] = "{query}"
        return urlunparse(p._replace(path="/".join(segs)))
    if token in p.fragment:
        return url_after.replace(token, "{query}", 1)
    return None


def _real_options(f: SearchField) -> tuple[list[str], list[str]]:
    vals, labs = [], []
    for v, l in f.options:
        if is_placeholder_option(v, l) or v in vals:
            continue
        vals.append(v)
        labs.append(l)
    return vals, labs


async def _search_html(session, s: Search, q: str) -> tuple[str, str]:
    """The result page of one query (html, final URL)."""
    page = await session.new_page()
    try:
        if s.how == "url":
            await B.goto(page, fill_template(s.url_template, q), settle_ms=2000)
        else:
            await B.goto(page, s.page_url, settle_ms=1500)
            await search_in_page(page, s, q)
        await page.evaluate(MARK_HIDDEN_JS)
        return await page.content(), page.url
    finally:
        await page.close()


def _region_keys(html: str) -> set:
    """Region signatures: selector, count and first record's text. The text counts too: a filtered list can
    come with the same card layout and page size but different content (category select, empty search)."""
    import lxml.html
    raw = lxml.html.document_fromstring(html or "<html><body></body></html>")
    return {(r.selector, r.count, r.sample_text) for r in find_regions(clean_tree(html), raw)}


def _result_score(html: str, layout: set) -> int:
    """Estimated record count on the (first) result page: regions found unchanged on the no-results page or
    on the search page itself (menu, footer, showcase and the like) are not counted."""
    import lxml.html
    raw = lxml.html.document_fromstring(html or "<html><body></body></html>")
    best = 0
    for r in find_regions(clean_tree(html), raw):
        if (r.selector, r.count, r.sample_text) in layout or r.distinct_links < 2:
            continue
        best = max(best, r.count)
    return best


def _substitute_note(probe: str, terms: list[str]) -> str:
    shown = f"'{probe}'" if probe else "an empty search"
    if terms and probe not in terms:
        return (f"This page is the site's SEARCH RESULT page, but because the user's term ({', '.join(map(repr, terms))}) "
                f"gives few results, a BROAD query ({shown}) was searched so that pagination is visible. The records "
                f"are not expected to relate to the term: judge records_match only by the record KIND (e.g. video, "
                f"profile, product). The user's term will be searched at run time. Leave search_needed=false.")
    return (f"This page is the site's SEARCH RESULT page ({shown} was searched). The rule reads the result records "
            f"of each search; the search itself is set up separately and the system sets url_pattern. Leave "
            f"search_needed=false. If there is navigation between result pages (pagination), write it too.")


async def _setup_search(session, url: str, sp: SimplePage, draft: RuleDraft, say) -> tuple[Optional[SearchSetup], str]:
    """Pick the search field, find out how the search works (URL template or form), and pick the result page
    to learn on. Returns (setup, error message)."""
    fields = sp.search_fields
    f = next((x for x in fields if x.index == draft.search_field), None) \
        or next((x for x in fields if x.kind == "text"), None) or (fields[0] if fields else None)
    if f is None:
        return None, "no search box found on the page"
    terms = [t.strip() for t in draft.search_terms if t and t.strip()]
    say(f"Search: field {f.index} ({f.kind}, {f.selector}), terms: {terms or 'none → all will be enumerated'}")
    form = Search(how="form", page_url=url, input=Sel(kind="css", value=f.selector),
                  submit=Sel(kind="css", value=f.submit) if f.submit else None, is_select=f.kind == "select")
    opts, labs = _real_options(f) if f.kind == "select" else ([], [])
    if f.kind == "select":
        if not opts:
            return None, "the select has no options"
        form.option_values, form.option_labels = opts, labs
        low = {l.lower(): v for v, l in zip(opts, labs)} | {v.lower(): v for v in opts}
        mapped = [low[t.lower()] for t in terms if t.lower() in low]
        if terms and not mapped:
            say(f"  terms are not among the options ({terms}); all options will be enumerated")
        terms = mapped
        token = terms[0] if terms else opts[0]
    else:
        token = NONSENSE
    # Mechanism: search in the browser; if the URL contains the term, a template (may also work over http), else a form
    page = await session.new_page()
    try:
        await B.goto(page, url, settle_ms=1500)
        await page.evaluate(MARK_HIDDEN_JS)
        start_html = await page.content()
        fields_before = {x.selector for x in sp.search_fields}
        before = page.url
        await search_in_page(page, form, token)
        after = page.url
        after_html = await page.content()
    except Exception as e:
        return None, f"search attempt failed: {type(e).__name__}: {_short(str(e), 150)}"
    finally:
        await page.close()
    tpl = _template_from(after, token, f.name) if after != before or token in after else None
    if tpl:
        s = Search(**{**form.model_dump(), "how": "url", "url_template": tpl})
        say(f"  search URL: {tpl}")
        if not await B.robots_allowed(fill_template(tpl, "a")):
            return None, f"robots.txt disallows the search result pages ({fill_template(tpl, 'a')})"
    else:
        s = form
        say("  the URL does not change on search; searches will fill the form in the browser")
    # Regions found unchanged on the no-results (NONSENSE) search (menu, footer) are not results. The search
    # page's own list is not subtracted: an empty search may list everything (scrapethissite: search page = full list).
    start_keys = _region_keys(start_html)
    layout = _region_keys(after_html) if f.kind == "text" else set()

    async def score(q: str) -> int:
        try:
            html, final = await _search_html(session, s, q)
        except Exception as e:
            say(f"  search '{q}' could not be opened: {type(e).__name__}")
            return 0
        if s.how == "url" and term_from_url(s, final) is None:
            say(f"  search '{q}' redirected to another URL ({final}); not counted")
            return 0
        if s.how == "form" and _region_keys(html) == start_keys:
            return 0  # nothing was submitted (e.g. a required field with an empty term): the page stayed the same
        return _result_score(html, layout)

    method, extra, probe = None, [], None
    if f.kind == "select":
        probe = terms[0] if terms else opts[0]
        # regions with the same content before the selection (menu, unselected list) are not counted
        n = _result_score(after_html, start_keys) if s.how == "form" or token == probe else await score(probe)
        say(f"  selecting '{labs[opts.index(probe)] if probe in opts else probe}': ~{n} records (first result page)")
        if n < 1:
            new_fields = [x for x in find_search_fields_html(after_html) if x.selector not in fields_before]
            hint = (f" After the selection another select was filled ({new_fields[0].selector}); the site may "
                    f"need a second selection for results. Dependent selects like this are not supported yet."
                    if new_fields else "")
            return None, f"searching by choosing from the select gave no results.{hint}"
        extra = terms[1:2] if terms else opts[1:2]
        method = None if terms else "options"
        s.enum = Enumeration(method="options", options=opts, labels=labs) if not terms else None
    elif terms:
        probe = terms[0]
        n_term = await score(probe)
        say(f"  search '{probe}': ~{n_term} records (first result page)")
        if n_term < FEW_RESULTS:
            n_a = await score("a")
            if n_a > n_term:
                say(f"  broad query 'a': ~{n_a} records; the rule will be learned on this result page and also tested with '{probe}'")
                extra, probe = [probe], "a"
            elif n_term < 1:
                return None, f"search '{probe}' gave no results; cannot learn the result page"
    else:
        path_tpl = s.how == "url" and "{query}" not in s.url_template.split("?", 1)[-1]
        for q in ALL_PROBES:
            if q == "" and path_tpl:
                continue  # /ara/{query}/ makes a meaningless URL with an empty term
            n = await score(q)
            say(f"  search '{q}': ~{n} records (first result page)")
            if n >= 3:
                probe = q
                method = {"": "empty", "*": "wildcard"}.get(q, "prefix")
                break
        if probe is None:
            return None, "empty search, '*' and letter prefixes gave no results; name the term to search in the request"
        s.enum = Enumeration(method=method, min_chars=max(1, len(probe)) if method == "prefix" else 1)
        lang = re.search(r"<html[^>]*\blang=[\"']?([a-zA-Z]{2})", start_html or "")
        if lang and lang.group(1).lower() != "tr":
            s.enum.alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"  # on a non-Turkish site ç, ğ … are wasted queries
        if method == "prefix":
            extra = ["e" if probe != "e" else "a"]
    s.terms = "given" if terms else "all"
    s.queries = terms
    rsp = None
    for attempt in (1, 2):  # a one-off timeout should not drop the search
        try:
            rsp = await _result_snapshot(session, s, probe)
            break
        except Exception as e:
            if attempt == 2:
                return None, f"result page could not be opened: {type(e).__name__}"
            say(f"  result page could not be opened ({type(e).__name__}); retrying")
    rsp.search_fields = []
    rsp.context_note = _substitute_note(probe if not s.is_select else (labs[opts.index(probe)] if probe in opts else probe),
                                        [labs[opts.index(t)] if t in opts else t for t in terms] if s.is_select else terms)
    return SearchSetup(search=s, probe=probe, sp=rsp, terms=terms, extra=extra, method=method), ""


async def _result_snapshot(session, s: Search, q: str) -> SimplePage:
    if s.how == "url":
        return await snapshot_page(session, fill_template(s.url_template, q))
    return await snapshot_page(session, s.page_url, action=lambda pg: search_in_page(pg, s, q))


def find_search_fields_html(html: str) -> list[SearchField]:
    import lxml.html
    from .simplify import find_search_fields
    return find_search_fields(lxml.html.document_fromstring(html or "<html><body></body></html>"))


async def learn(url: str, goal: str, *, name: Optional[str] = None, also: list[str] = (),
                max_iter: int = 8, test_pages: int = 3, model: str = DEFAULT_MODEL,
                headless: bool = True, say: Callable[[str], None] = print,
                search: Literal["auto", "on", "off"] = "auto") -> LearnResult:
    """search: auto = switch to search if the LLM asks for it, or if the records are not on the page and there
    is a search box; on = search is required; off = search is not used."""
    name = name or slug(f"{site_of(url)}-{goal}")
    out = LearnResult(rule=None, accepted=False)
    async with B.BrowserSession(headless=headless) as session:
        sp = await snapshot_page(session, url)
        say(f"Page simplified: {sp.raw_chars:,} → {len(sp.dom):,} chars, "
            f"{len(sp.regions)} repeated regions, {len(sp.controls)} button/link candidates")
        prompt, schema = initial_prompt(goal, sp), DRAFT_SCHEMA
        hint_values = _hint_values(sp)
        if hint_values:
            say(f"Record count hints on the page: {', '.join(map(str, hint_values[:5]))}")
        last: Optional[Attempt] = None
        not_found = 0
        setup: Optional[SearchSetup] = None
        stable_checked = False
        search_err = ""
        search_state = "off" if search == "off" else "pending"
        if search == "on" and not sp.search_fields:
            out.failure = "No search box found on the page (search was set to required). Give the page that has the search box."
            return out
        if sp.search_fields and search_state == "pending":
            say(f"Search field candidates: {len(sp.search_fields)}")
        for it in range(1, max_iter + 1):
            m = model if it <= 4 else ESCALATION_MODEL
            say(f"[{it}/{max_iter}] {m} thinking…")
            try:
                res = await ask_json(prompt, schema, m)
            except LLMError as e:
                say(f"  LLM error: {e}")
                continue
            out.cost_usd += res.cost_usd
            out.llm_calls += 1
            data = res.data
            if schema is REVIEW_SCHEMA:
                verdict = data.get("verdict")
                say(f"  verdict: {verdict} — {_short(data.get('reason', ''))}")
                if verdict == "accept":
                    if last and not last.issues and setup is not None and not stable_checked:
                        # On some sites search results come in a different layout from visit to visit (bkmkitap):
                        # open the same search again, does the rule still give records?
                        stable_checked = True
                        r2 = await run_rule(last.rule, url, session, max_pages=1, queries=[setup.probe])
                        say(f"  stability check: the same search opened again, {len(r2.items)} records")
                        if not r2.items:
                            alt = ""
                            try:
                                alt = (await _result_snapshot(session, setup.search, setup.probe)).as_prompt(max_regions=4)[:6000]
                            except Exception:
                                pass
                            last.issues = ["When the same search was opened again the rule gave 0 records; the site may "
                                           "show a different result layout on different visits."]
                            prompt = review_prompt(
                                goal, last.rule, last.report, last.issues, sp,
                                note="The same search, opened again, gave 0 records with this rule. The site seems to "
                                     "serve DIFFERENT result layouts on different visits. Make item, fields and wait_for "
                                     "cover both layouts (CSS selector lists 'a, b'; use pagination that exists in both "
                                     "or the more common one). The second visit looked like this:\n" + alt)
                            continue
                    if last and not last.issues:
                        out.rule, out.accepted = last.rule, True
                        break
                    prompt = review_prompt(goal, last.rule, last.report, last.issues, sp,
                                           note="You said accept, but automatic checks failed. Fix the rule.")
                    continue
                data = data.get("rule")
                if not data:
                    prompt = review_prompt(goal, last.rule, last.report, last.issues, sp,
                                           note="verdict=revise requires the full corrected rule in 'rule'.")
                    continue
            try:
                draft = RuleDraft.model_validate(data)
            except ValidationError as e:
                say("  rule did not match the schema, asking for a fix")
                prompt = (f"Your previous output did not validate against the schema:\n{e}\n\n"
                          f"Previous output:\n{json.dumps(data, ensure_ascii=False)[:6000]}\n\n"
                          + initial_prompt(goal, sp))
                schema = DRAFT_SCHEMA
                continue
            # Search is set up only when the LLM asks for it (or it is required). It is not started just because
            # the records are not on the page: the wrong page may have been given; the LLM is reminded of the
            # search box instead (below).
            if search_state == "pending" and sp.search_fields and (draft.search_needed or search == "on"):
                why = "the LLM said a search is needed" if draft.search_needed else "search was set to required"
                say(f"Switching to search mode ({why}).")
                setup, err = await _setup_search(session, url, sp, draft, say)
                if setup is None:
                    say(f"  Search could not be set up: {err}")
                    search_state, search_err = "failed", err
                    if search == "on" or not draft.records_match or not sp.regions:
                        # the records are not on this page and search failed too: do not keep testing a rule with no results
                        out.failure = f"Search could not be set up: {err}"
                        break
                    say("  The records are listed on this page too; continuing without search.")
                else:
                    search_state = "on"
                    out.attempts.clear()  # attempts without search must not be chosen over the search rule
                    last = None
                    sp = setup.sp
                    hint_values = _hint_values(sp)
                    not_found = 0
                    say(f"Result page simplified (search '{setup.probe}'): {sp.raw_chars:,} → {len(sp.dom):,} "
                        f"chars, {len(sp.regions)} repeated regions")
                    prompt, schema = initial_prompt(goal, sp), DRAFT_SCHEMA
                    continue
            elif search_state == "pending" and draft.records_match:
                search_state = "off"
            elif search_state == "failed" and draft.search_needed and (last is None or last.total == 0):
                # search could not be set up, the LLM still wants it and the attempt without search gives no records: stop spending
                out.failure = f"Search could not be set up: {search_err}"
                break
            rule = _to_rule(draft, name=name, url=url, goal=goal, also=also)
            if setup is not None:
                rule.search = setup.search.model_copy(deep=True)
                rule.url_pattern = choose_url_pattern(None, url, [])
            for r in rule.requested:
                say(f"  requested '{r.asked}' → {r.field or 'NONE'}")
            if not rule.records_match and not rule.in_linked_pages:
                not_found += 1
                say(f"  ! LLM: the requested records ('{rule.records_asked}') are not on this page — {_short(rule.goal_fields_note)}")
                if not_found >= 2:  # same answer twice: stop instead of inventing other records
                    out.failure = (f"The requested data ('{rule.records_asked}') was not found on this page: "
                                   f"{_short(rule.goal_fields_note, 400)} Give the URL of the page that lists this data.")
                    break
                hint = ("If these records can only be found through one of the SEARCH FIELD CANDIDATES (a search box "
                        "or search form for them), set search_needed=true with search_field and search_terms. "
                        if search_state == "pending" and sp.search_fields else "")
                prompt = (f"{_goal_block(goal)}\nYou said records_match=false: {rule.goal_fields_note}\n"
                          "Look at the page once more. If the requested records really are on it, write the rule "
                          f"for them with records_match=true. {hint}If not, answer records_match=false again and do "
                          "not substitute other records.\n\n" + initial_prompt(goal, sp))
                schema = DRAFT_SCHEMA
                continue
            say(f"  testing: item={rule.item.value!r}, pagination={rule.pagination.type}")
            probe = [setup.probe] if setup else None
            run = await run_rule(rule, url, session, max_pages=test_pages, progress=lambda s: say("    " + s),
                                 queries=probe)
            issues = check(rule, run, partial=True, hint_values=hint_values)
            for q in (setup.extra if setup else []):
                run2 = await run_rule(rule, url, session, max_pages=min(2, test_pages), queries=[q])
                say(f"    search '{q}': {len(run2.items)} records")
                issues += [f"[search '{q}'] {i}" for i in check(rule, run2, partial=False, limited=True)
                           if i not in issues]
            for u in ([] if setup else also):
                run2 = await run_rule(rule, u, session, max_pages=min(2, test_pages))
                issues += [f"[extra page {u}] {i}" for i in check(rule, run2, partial=True, strict_pagination=False)
                           if i not in issues]
            rep = report(rule, run)
            last = Attempt(rule=rule, issues=issues, total=len(run.items), report=rep, run=run)
            out.attempts.append(last)
            say(f"  result: {len(run.items)} unique items, fill rates {rep.get('fill_rates', rep.get('doluluk'))}")
            for i in issues:
                say(f"  ! {i}")
            prompt, schema = review_prompt(goal, rule, rep, issues, sp), REVIEW_SCHEMA

        if out.failure:
            return out
        if not out.accepted:
            clean = [a for a in out.attempts if not a.issues]
            if clean:
                best = max(clean, key=lambda a: a.total)
                out.rule = best.rule
                say("Warning: the LLM did not give final approval; the best attempt that passed the strict checks was chosen.")
            elif out.attempts:
                out.rule = max(out.attempts, key=lambda a: a.total).rule
                say("Warning: no attempt fully passed the checks; the best attempt will be saved as UNVERIFIED.")

        if out.rule is not None:
            chosen = next((a for a in out.attempts if a.rule is out.rule), None)
            if chosen and chosen.run and _pattern_hits_items(out.rule, chosen.run.items):
                out.rule.url_pattern = default_url_pattern(url, list(also))
                if _pattern_hits_items(out.rule, chosen.run.items):
                    paths = "|".join(dict.fromkeys(re.escape(urlparse(u).path) for u in [url, *also]
                                                   if site_of(u) == site_of(url)))
                    out.rule.url_pattern = r"^https?://(www\.)?" + re.escape(site_of(url)) + r"(:\d+)?(?:" \
                        + paths + r")(?:[?#]|$)"
                say(f"URL pattern also matched product/detail links; narrowed: {out.rule.url_pattern}")
            unmatched = [u for u in also if not re.search(out.rule.url_pattern, u)]
            if unmatched:
                say(f"Warning: the URL pattern does not match these extra pages, use --rule {out.rule.name} for them: "
                    + ", ".join(unmatched))
            if chosen and chosen.run:
                _require_requested(out.rule, chosen.run.items, say)
            await _try_http(out.rule, url, chosen.run if chosen else None, test_pages, say,
                            queries=[setup.probe] if setup else None)
            if setup is not None and out.rule.search is not None:
                _describe_search(out.rule, setup, say)
            if chosen and chosen.run and any(not r.field for r in out.rule.requested):
                try:
                    await _learn_detail(out, chosen.run.items, goal, session, model, say)
                except Exception as e:  # the list rule is still saved
                    out.rule.detail = None
                    say(f"Warning: the record page step failed ({type(e).__name__}: {_short(str(e))}); not added.")
            got = {r.asked for r in (out.rule.detail.requested if out.rule.detail else []) if r.field}
            missing = [r.asked for r in out.rule.requested if not r.field and r.asked not in got]
            if missing:
                say(f"Warning: requested {', '.join(repr(m) for m in missing)} not found; not in the rule.")
            out.rule.meta.update({
                "learned_at": now_iso(), "sample_url": url, "also": list(also), "accepted": out.accepted,
                "verified": any(a.rule is out.rule and not a.issues for a in out.attempts),
                "attempts": len(out.attempts), "llm_calls": out.llm_calls,
                "missing_requested": missing,
                **({"search_probe": setup.probe} if setup else {}),
                "cost_usd_estimate": round(out.cost_usd, 4)})
    return out


DETAIL_SCHEMA = DetailDraft.model_json_schema()


class DetailReview(BaseModel):
    verdict: Literal["accept", "revise"]
    reason: str = Field(description="Short reason")
    rule: Optional[DetailDraft] = Field(default=None, description="if verdict=revise: the corrected FULL detail rule")


DETAIL_REVIEW_SCHEMA = DetailReview.model_json_schema()
DETAIL_TEST = 3  # number of record pages opened while learning


def _link_field(rule: Rule, items: list[dict]) -> Optional[str]:
    """The field holding the link to the record's own page: values start with http and differ per record."""
    cands = [rule.expect.unique_by] + [f.name for f in rule.fields if f.attr == "href"] + [f.name for f in rule.fields]
    for name in dict.fromkeys(c for c in cands if c):
        vals = [it.get(name) for it in items[:20]]
        urls = [v for v in vals if isinstance(v, str) and v.startswith("http")]
        if len(urls) >= 0.8 * max(1, len(vals)) and len(set(urls)) > 0.8 * len(urls):
            return name
    return None


def _detail_prompt(goal: str, rule: Rule, sample: dict, missing: list[str], sp: SimplePage) -> str:
    return (f"{_goal_block(goal)}\nMISSING requested data (not on the list page): "
            f"{json.dumps(missing, ensure_ascii=False)}\nList record already extracted: "
            f"{json.dumps(sample, ensure_ascii=False)[:800]}\nList field names (do not reuse): "
            f"{[f.name for f in rule.fields]}\n\n{DETAIL_GUIDE}\n=== RECORD PAGE ({sp.url}) ===\n{sp.as_prompt()}\n\n"
            f"{_goal_block(goal)}")


async def _test_detail(det: Detail, items: list[dict], session) -> tuple[RunResult, dict]:
    import copy
    rr = RunResult(url="", items=copy.deepcopy(items[:DETAIL_TEST]))
    await enrich(det, rr, session)
    rates = {}
    for f in det.fields:
        seen = [it for it in rr.items if f.name in it]
        rates[f.name] = round(sum(1 for it in seen if it[f.name] not in (None, "", [])) / len(seen), 2) if seen else 0
    return rr, rates


async def _learn_detail(out: LearnResult, items: list[dict], goal: str, session, model: str, say) -> None:
    """If requested data is not on the list page, learn an extra rule from the record's own page (one level)."""
    rule = out.rule
    missing = [r.asked for r in rule.requested if not r.field]
    lf = _link_field(rule, items)
    if not lf:
        return
    links = [it[lf] for it in items if isinstance(it.get(lf), str)][:DETAIL_TEST]
    if not await B.robots_allowed(links[0]):
        say(f"Record page disallowed by robots.txt: {links[0]}")
        return
    say(f"Looking for requested {', '.join(repr(m) for m in missing)} on the record's own page (one level): {links[0]}")
    try:
        sp = await snapshot_page(session, links[0])
    except Exception as e:
        say(f"  record page could not be opened: {type(e).__name__}")
        return
    prompt, schema = _detail_prompt(goal, rule, items[0], missing, sp), DETAIL_SCHEMA
    names = {f.name for f in rule.fields}
    best: Optional[Detail] = None
    for it in range(1, 5):
        say(f"[record page {it}/4] {model} thinking…")
        try:
            res = await ask_json(prompt, schema, model)
        except LLMError as e:
            say(f"  LLM error: {e}")
            continue
        out.cost_usd += res.cost_usd
        out.llm_calls += 1
        data = res.data
        if schema is DETAIL_REVIEW_SCHEMA:
            say(f"  verdict: {data.get('verdict')} — {_short(data.get('reason', ''))}")
            if data.get("verdict") == "accept" and best is not None and not issues:
                rule.detail = best
                break
            if data.get("verdict") == "accept" or not data.get("rule"):
                # Ask for a fix instead of retrying the same rule with the same prompt.
                why = ("the automatic checks FAILED (see PROBLEMS), so accept is not possible"
                       if data.get("verdict") == "accept" else "verdict=revise came without a rule")
                if prompt.startswith("NOTE: "):  # do not stack notes
                    prompt = prompt.split("\n\n", 1)[1]
                prompt = f"NOTE: {why}. Answer verdict=revise WITH the FULL corrected rule.\n\n" + prompt
                continue
            data = data["rule"]
        try:
            draft = DetailDraft.model_validate(data)
        except ValidationError as e:
            prompt = f"Your output did not validate:\n{e}\n\n" + _detail_prompt(goal, rule, items[0], missing, sp)
            schema = DETAIL_SCHEMA
            continue
        if not draft.found or not draft.fields:
            say(f"  not on the record page either — {_short(draft.note)}")
            return
        det = Detail(**draft.model_dump(), link_field=lf)
        try:
            rr, rates = await _test_detail(det, items, session)
        except Exception as e:
            say(f"  record page test failed: {type(e).__name__}: {_short(str(e))}")
            issues = [f"running the rule failed: {type(e).__name__}: {e}"]
            prompt = (f"{_goal_block(goal)}\nYou wrote this record-page rule:\n"
                      f"{json.dumps(draft.model_dump(exclude_none=True), ensure_ascii=False)}\n\nPROBLEMS:\n- "
                      f"{issues[0]}\nFix it and write the FULL corrected rule.\n\n"
                      + _detail_prompt(goal, rule, items[0], missing, sp))
            schema = DETAIL_SCHEMA
            continue
        issues = [f"field name '{f.name}' is already used by the list rule; rename it"
                  for f in det.fields if f.name in names]
        issues += [f"requested '{r.asked}' -> '{r.field}' is not in fields" for r in det.requested
                   if r.field and r.field not in {f.name for f in det.fields}]
        issues += [f"requested '{r.asked}' -> '{r.field}' filled on {rates.get(r.field, 0) * 100:.0f}% of "
                   f"{len(rr.items)} record pages" for r in det.requested
                   if r.field in rates and rates[r.field] < 0.66]
        issues += [e for e in rr.errors][:5]
        for r in det.requested:
            say(f"  requested '{r.asked}' → {r.field or 'NONE'} (record page, fill rate {rates.get(r.field, 0)})")
        best = det
        samples = [{f.name: (v[:5] if isinstance(v := x.get(f.name), list) else v) for f in det.fields}
                   for x in rr.items]
        prompt = (f"{_goal_block(goal)}\nYou wrote this record-page rule:\n"
                  f"{json.dumps(draft.model_dump(exclude_none=True), ensure_ascii=False)}\n\nTested on "
                  f"{len(rr.items)} record pages. Fill rates: {rates}\nSamples: "
                  f"{json.dumps(samples, ensure_ascii=False)[:2500]}\nPROBLEMS:\n"
                  + ("\n".join(f"- {i}" for i in issues) if issues else "- none") +
                  "\n\nverdict=accept only if the values are exactly the missing data the user asked for and "
                  "there are no problems; otherwise revise with the FULL corrected rule.\n\n"
                  + _detail_prompt(goal, rule, items[0], missing, sp))
        schema = DETAIL_REVIEW_SCHEMA
    if rule.detail is None and best is not None and not issues:
        rule.detail = best
        say("Warning: the LLM did not give final approval to the record page rule; the last attempt that passed the checks was used.")
    if rule.detail is None:
        say("Warning: the record page rule could not be verified; not added.")
        return
    # If record pages give the same data without a browser, use http (much faster)
    try:
        h = rule.detail.model_copy(update={"fetch": "http"})
        hr, hrates = await _test_detail(h, items, None)
        br, _ = await _test_detail(rule.detail, items, session)
        same = all(a.get(f.name) == b.get(f.name) for a, b in zip(hr.items, br.items) for f in h.fields)
        if same and all(v >= 0.66 for v in hrates.values()):
            rule.detail.fetch = "http"
    except Exception:
        pass
    per = await detail_sec_per_page(rule.detail, links[0], rule.delay_ms)
    rule.meta["detail_sec_per_page"] = round(per, 2)
    say(f"Record page rule added: fields {[f.name for f in rule.detail.fields]}, fetch={rule.detail.fetch}.")
    say(f"Warning: this rule also opens the own page of EVERY record from the list ({fmt_duration(per)} per record; "
        f"every 100 records add {fmt_duration(100 * per)}). You can shorten this with a page limit: fewer list "
        f"pages = fewer record pages.")


def _require_requested(rule: Rule, items: list[dict], say) -> None:
    """Make requested single-value fields required if they were almost always filled in the test (so runs
    check them). Sparsely filled fields (like a discount on only some products) stay optional: no false alarms."""
    rates = fill_rates(rule, items)
    asked = {r.field for r in rule.requested if r.field}
    made = []
    for f in rule.fields:
        if f.name in asked and not f.multiple and not f.required and rates.get(f.name, 0) >= 0.9:
            f.required = True
            made.append(f.name)
    if made:
        say(f"Requested fields made required: {', '.join(made)}")


def _hint_values(sp: SimplePage) -> list[int]:
    vals = []
    for h in sp.count_hints:
        m = COUNT_RE.search(h)
        if m:
            v = parse_number(m.group(1))
            if isinstance(v, (int, float)) and 0 < v < 10_000_000:
                vals.append(int(v))
    return sorted(set(vals))


def _same_values(rule: Rule, a: list[dict], b: list[dict]) -> bool:
    """Do the HTTP and browser results give the same records with the same values (order does not matter;
    live-sorted lists can shift slightly)?"""
    col_a, col_b = _key_map(rule, a), _key_map(rule, b)
    shared = set(col_a) & set(col_b)
    if not col_a or len(shared) < 0.8 * len(col_a):
        return False
    return all(col_a[k] == col_b[k] for k in shared)


def _key_map(rule: Rule, items: list[dict]) -> dict:
    k = rule.expect.unique_by
    return {json.dumps(it.get(k) if k else it, ensure_ascii=False, sort_keys=True): it for it in items}


def _pattern_hits_items(rule: Rule, items: list[dict]) -> bool:
    """Does url_pattern also match the extracted records' own links (detail pages)?"""
    try:
        rx = re.compile(rule.url_pattern)
    except re.error:
        return False
    lf = _link_field(rule, items)
    names = [lf] if lf else [rule.expect.unique_by] + [f.name for f in rule.fields if f.attr == "href"]
    urls = [v for it in items[:50] for n in names if n and isinstance(v := it.get(n), str) and v.startswith("http")]
    return bool(urls) and sum(1 for u in urls if rx.search(u)) > 0.5 * len(urls)


async def _try_http(rule: Rule, url: str, browser_run, test_pages: int, say, queries=None) -> None:
    """If the rule gives the same result without a browser (plain HTTP), set fetch=http: much faster."""
    if rule.search is not None and rule.search.how == "form":
        say("Browser required (search works by filling a form).")
        return
    blocking = [s for s in rule.setup if not s.optional]
    if rule.pagination.type in ("infinite_scroll", "load_more") or blocking:
        say("Browser required (dynamic pagination or a required setup step).")
        return
    if browser_run is None or not browser_run.items:
        return
    try:
        h = await run_rule(rule, url, None, max_pages=test_pages, force_fetch="http", queries=queries)
    except Exception as e:
        say(f"Browser required (HTTP test failed: {type(e).__name__}).")
        return
    b = browser_run
    if len(h.items) >= 0.9 * len(b.items) and _same_values(rule, b.items, h.items) and not check(rule, h, partial=True):
        rule.fetch = "http"
        say(f"Fast mode: the page gives the same data without a browser ({len(h.items)}/{len(b.items)}) → fetch=http")
    else:
        say(f"Browser required (HTTP gave {len(h.items)}/{len(b.items)} items or different values).")


def _describe_search(rule: Rule, setup: SearchSetup, say) -> None:
    s = rule.search
    how = f"URL template {s.url_template}" if s.how == "url" else "by filling the form (browser)"
    if s.terms == "given":
        say(f"Search rule: {how}. Default terms: {', '.join(map(repr, s.queries))}. Other terms can be given "
            f"at run time (--query).")
        return
    e = s.enum
    if e.method == "empty":
        what = "an empty search lists all records"
    elif e.method == "wildcard":
        what = f"searching '{e.wildcard}' lists all records"
    elif e.method == "options":
        what = f"each of the {len(e.options)} select options will be searched"
    else:
        n = len(e.alphabet) ** max(1, e.min_chars)
        what = (f"since no specific name was given, {e.min_chars}-letter prefixes will be enumerated (at least {n} "
                f"queries); a prefix that hits the result cap is extended by one letter (at most {e.max_depth} letters)")
    say(f"Search rule: {how}; {what}. Default query limit {s.max_queries} (0 = unlimited).")
