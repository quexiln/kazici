"""Checks a run result against the rule's expectations (no LLM, deterministic checks)."""
from __future__ import annotations

import re

from .executor import RunResult, fmt_duration
from .rules import Rule, goal_issues

# Error messages from executor/extract/browser are matched by these fragments (lowercase).
_DETAIL_OPEN_FAILED = ("could not open record page",)
_RULE_ERROR_KEYS = ("invalid", "not found", "setup step", "error", "could not open")
_SEARCH_RULE_ERROR_KEYS = ("invalid", "setup step")
_SEARCH_FAIL_PREFIXES = ("search failed", "HTTP", "could not open page", "run error")


def fill_rates(rule: Rule, items: list[dict]) -> dict[str, float]:
    if not items:
        return {f.name: 0.0 for f in rule.fields}
    out = {f.name: round(sum(1 for it in items if it.get(f.name) not in (None, "", [])) / len(items), 3)
           for f in rule.fields}
    if rule.detail:  # detail fields are measured only on records whose page was opened (those that have the key)
        for f in rule.detail.fields:
            seen = [it for it in items if f.name in it]
            out[f.name] = round(sum(1 for it in seen if it[f.name] not in (None, "", [])) / len(seen), 3) \
                if seen else 0.0
    return out


def detail_issues(rule: Rule, res: RunResult, need: float = 0.5) -> list[str]:
    """If the requested fields from record pages are mostly empty, the detail rule may be broken."""
    if not rule.detail or not res.detail_visited:
        return []
    rates = fill_rates(rule, res.items)
    asked = {r.field for r in rule.detail.requested if r.field}
    return [f"Record page field '{f.name}' fill rate is {rates.get(f.name, 0) * 100:.0f}% "
            f"(at least {need * 100:.0f}%); the record page layout may have changed."
            for f in rule.detail.fields if f.name in asked and rates.get(f.name, 0) < need]


def check(rule: Rule, res: RunResult, partial: bool, hint_values: list[int] | None = None,
          strict_pagination: bool | None = None, limited: bool = False) -> list[str]:
    """partial=True: during learning only a few pages were tried; min_total is not exact but estimated.
    partial=False: run mode (lenient). limited=True: the user set a page limit, min_total is not checked.
    hint_total: total record count written on the page ("1,016 products"); catches missing pagination when learning."""
    issues: list[str] = goal_issues(rule) if partial else []
    if rule.search is not None and not partial:
        return issues + search_run_issues(rule, res)
    ex = rule.expect
    strict_pagination = partial if strict_pagination is None else strict_pagination
    first_count = res.first_page_count
    bigger = [v for v in (hint_values or []) if v > max(first_count, 1) * 1.5]
    hint_total = min(bigger) if bigger else None  # the hint closest to the list (not site-wide counts)
    first = res.first_page_count
    pg = rule.pagination
    grew = len(res.items) > first or any(p.new > 0 for p in res.pages[1:])
    # In a run, if the list ended naturally on the first page (small category), few items is not an error
    small_list = not partial and res.items and not grew and (pg.type == "none" or res.natural_end)
    if first < ex.min_items_per_page and not small_list:
        issues.append(f"{first} items matched on the first page, expected at least {ex.min_items_per_page}.")
    if not res.items:
        issues.append("No data extracted (all fields empty or the item selector matched nothing).")
    rates = fill_rates(rule, res.items)
    for f in rule.fields:
        if not f.required:
            continue
        # In a run, fields other than the record identity (unique_by) may genuinely be empty (e.g. a product without a price)
        need = ex.min_fill_rate if (partial or f.name == ex.unique_by) else min(ex.min_fill_rate, 0.5)
        if rates.get(f.name, 0) < need:
            issues.append(f"Required field '{f.name}' fill rate is {rates[f.name] * 100:.0f}% "
                          f"(at least {need * 100:.0f}%).")
    for f in rule.fields:
        if not partial or not f.required or f.attr != "text" or f.multiple:
            continue
        vals = [it.get(f.name) for it in res.items if isinstance(it.get(f.name), str)]
        cut = sum(1 for v in vals if v.endswith(("...", "…")))
        if vals and cut / len(vals) > 0.2:
            issues.append(f"{cut / len(vals) * 100:.0f}% of '{f.name}' values end with '...': the visible text is "
                          f"truncated. Use an attribute such as title / aria-label for the full value.")
    for name, pat in ex.field_patterns.items():
        vals = []
        for it in res.items:
            v = it.get(name)
            vals.extend(str(x) for x in (v if isinstance(v, list) else [v]) if x not in (None, ""))
        if not vals:
            continue
        try:
            ok = sum(1 for v in vals if re.search(pat, v)) / len(vals)
        except re.error as e:
            issues.append(f"Invalid pattern for '{name}': {e}")
            continue
        if ok < 0.9:
            bad = [v for v in vals if not re.search(pat, v)][:3]
            issues.append(f"Only {ok * 100:.0f}% of '{name}' values match /{pat}/. Examples: {bad}")
    if ex.unique_by and ex.unique_by not in {f.name for f in rule.fields}:
        issues.append(f"unique_by='{ex.unique_by}' is not one of the fields.")
    elif partial and ex.unique_by and res.pages and res.pages[0].rows >= 5 \
            and res.pages[0].rows - res.pages[0].new > max(1, 0.1 * res.pages[0].rows):
        p0 = res.pages[0]  # rows: empty rows (ad slots) are not counted, only records actually extracted
        issues.append(f"unique_by='{ex.unique_by}' repeats on the same page (only {p0.new} unique out of {p0.rows} "
                      f"records on the first page): this field does not tell records apart, so records are lost. "
                      f"Pick a field that identifies a record on its own (usually the link) or remove unique_by.")
    if pg.type != "none":
        # a small single-page list: not an error if the pages ran out naturally (except the main learning page)
        single_page_ok = not strict_pagination and res.stop_reason in (
            "empty_page", "no_next_link", "no_next_button", "no_load_more_button", "nothing_loaded")
        # if the user asked for a single page, the next page was never tried: pagination cannot be tested
        one_page_only = limited and res.stop_reason == "max_pages" and len(res.pages) <= 1
        if not grew and not single_page_ok and not one_page_only:
            issues.append(f"Pagination ({pg.type}) brought no new items; next page/scrolling may not be working. "
                          f"Stop reason: {res.stop_reason}")
    total = len(res.items)
    # If pagination ended naturally (pages ran out) it may be a smaller category: not an error.
    if not partial and not limited and total < ex.min_total and not res.natural_end:
        issues.append(f"{total} unique items in total, expected at least {ex.min_total} "
                      f"(stop reason: {res.stop_reason}).")
    if partial and hint_total and hint_total > max(first, 1) * 1.5 and total < hint_total * 0.5 \
            and (pg.type == "none" or len(res.items) <= first):
        issues.append(f"The page says ~{hint_total} records in total but only {total} were extracted; pagination is "
                      f"missing or not working (do the page links have a parameter like ?page=, ?pg=, ?p=?).")
    if partial and pg.type == "none" and total < ex.min_total:
        issues.append(f"No pagination and {total} items in total; expected at least {ex.min_total}. "
                      f"Either pagination is missing or the expectation is wrong.")
    for e in res.errors:
        if e.lower().startswith(_DETAIL_OPEN_FAILED):
            continue  # not a problem one by one; the rate is checked below (from the counter, messages capped at 50)
        if any(k in e.lower() for k in _RULE_ERROR_KEYS):
            issues.append("Error: " + e)
    tried = res.detail_visited + res.detail_failed
    if tried and res.detail_failed > 0.2 * tried:
        issues.append(f"{res.detail_failed}/{tried} record pages could not be opened.")
    issues += detail_issues(rule, res, need=0.9 if partial else 0.5)
    return issues


def search_run_issues(rule: Rule, res: RunResult) -> list[str]:
    """Run check for a search rule. A term giving few or no results is not an error; per-page item,
    pagination and total expectations are specific to the learning query, so they are not checked."""
    issues = []
    if res.stop_reason in ("robots", "no_terms"):
        return [res.errors[0]] if res.errors else [res.stop_reason]
    if not res.items and (rule.search.terms == "all" or len(res.queries) >= 3):
        issues.append(f"None of the {len(res.queries)} searches returned results; the search or results page layout "
                      f"may have changed.")
    own = {q.lower() for q in rule.search.queries} | ({rule.meta["search_probe"].lower()}
                                                     if rule.meta.get("search_probe") else set())
    s = rule.search
    own |= {l.lower() for v, l in zip(s.option_values, s.option_labels) if v.lower() in own}  # select: labels
    dead = [x["query"] for x in res.queries if not x["items"] and not x.get("failed") and x["query"].lower() in own]
    if dead:  # a term that gave results when the rule was learned is now empty: the layout may have changed
        issues.append(f"The term the rule was learned with ({', '.join(map(repr, dead))}) now returned no results; the "
                      f"results page layout may have changed or the site may have shown a different layout.")
    if res.queries and res.search_failed > 0.2 * len(res.queries):
        first = next((e for e in res.errors if e.lower().startswith(tuple(p.lower() for p in _SEARCH_FAIL_PREFIXES))),
                     "")
        issues.append(f"Could not read the results page for {res.search_failed}/{len(res.queries)} searches. {first}".strip())
    if res.items:
        rates = fill_rates(rule, res.items)
        ex = rule.expect
        for f in rule.fields:
            need = ex.min_fill_rate if f.name == ex.unique_by else min(ex.min_fill_rate, 0.5)
            if f.required and rates.get(f.name, 0) < need:
                issues.append(f"Required field '{f.name}' fill rate is {rates[f.name] * 100:.0f}% (at least {need * 100:.0f}%).")
        for name, pat in ex.field_patterns.items():
            vals = [str(x) for it in res.items for x in (it.get(name) if isinstance(it.get(name), list)
                                                         else [it.get(name)]) if x not in (None, "")]
            try:
                ok = sum(1 for v in vals if re.search(pat, v)) / len(vals) if vals else 1
            except re.error:
                continue
            if ok < 0.9:
                issues.append(f"Only {ok * 100:.0f}% of '{name}' values match /{pat}/.")
    for e in res.errors:
        # per-query open/search failures were assessed as a rate above; a search without results never shows
        # the list, so a wait_for not being found is normal too
        if any(k in e.lower() for k in _SEARCH_RULE_ERROR_KEYS):
            issues.append("Error: " + e)
    tried = res.detail_visited + res.detail_failed
    if tried and res.detail_failed > 0.2 * tried:
        issues.append(f"{res.detail_failed}/{tried} record pages could not be opened.")
    return issues + detail_issues(rule, res, need=0.5)


def soft_notes(rule: Rule, res: RunResult) -> list[str]:
    """Situations that are not errors but should be told to the user."""
    notes = []
    if res.queries:
        empty = [q["query"] for q in res.queries if not q["items"]]
        notes.append(f"{len(res.queries)} searches done" + (f", {len(empty)} returned no results"
                                                             + (f" ({', '.join(map(repr, empty[:5]))})" if empty else "")
                                                             if empty else "") + ".")
        if res.stop_reason == "query_limit":
            notes.append(f"Query limit reached; at least {res.queries_left} more queries remained (raise the query "
                         f"limit or set 0 = unlimited).")
        if res.stop_reason == "max_pages":
            notes.append("Some searches hit the page limit or stopped early; there may be more results.")
        if any(q["stop_reason"] == "form_no_pagination" for q in res.queries):
            notes.append("The search uses a form and the URL does not change: only the first results page of each search was read.")
        if res.search_failed:
            notes.append(f"Could not read the results page for {res.search_failed} searches.")
        if res.detail_visited:
            notes.append(f"Opened the own page of {res.detail_visited} records.")
        return notes
    if len(res.items) < rule.expect.min_total and res.natural_end and res.url == rule.meta.get("sample_url"):
        notes.append(f"Pages ran out ({res.stop_reason}); {len(res.items)} items, the expectation on the learning page "
                     f"was {rule.expect.min_total} (may be a smaller list).")
    if res.stop_reason == "max_pages":
        notes.append(f"Page limit ({res.rounds}) reached; there may be more "
                     f"(use --all or page limit 'All' to fetch everything).")
    if res.detail_visited:
        notes.append(f"Opened the own page of {res.detail_visited} records"
                     + (f" (took {fmt_duration(res.detail_visited * res.detail_sec_per_page)})."
                        if res.detail_sec_per_page else "."))
    if res.detail_failed:
        notes.append(f"{res.detail_failed} record pages could not be opened; those records have no record page fields.")
    if res.delay_ms > rule.delay_ms:
        notes.append(f"Waited {res.delay_ms / 1000:.0f} s between pages because of the robots.txt Crawl-delay.")
    return notes


def report(rule: Rule, res: RunResult, n_head: int = 5, n_tail: int = 2) -> dict:
    items = res.items
    sample = items[:n_head] + (items[-n_tail:] if len(items) > n_head + n_tail else [])
    snap = rule.pagination.type in ("infinite_scroll", "load_more")
    return {
        "total_unique": len(items),
        "rounds": res.rounds,
        ("snapshots (total on the page in each round)" if snap else "pages"):
            [{"no": p.page, "matched": p.matched, "new": p.new, "url": p.url} for p in res.pages],
        "stop_reason": res.stop_reason,
        "fill_rates": fill_rates(rule, items),
        "samples": [{k: (v[:5] if isinstance(v, list) else (v[:200] if isinstance(v, str) else v))
                      for k, v in it.items()} for it in sample],
        "errors": res.errors[:10],
    }
