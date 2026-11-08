"""Run logic shared by the CLI and the web interface (run a rule without the LLM, save the result)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import browser as B
from .executor import RunResult, needs_browser, run_rule
from .rules import RULES_DIR, Rule, find_rule
from .validate import check, fill_rates, soft_notes

OUT_DIR = Path(__file__).resolve().parent.parent / "output"


@dataclass
class RunOutcome:
    payload: dict
    out_path: Path
    rows: list[dict] = field(default_factory=list)          # summary table rows
    broken: list[tuple[str, str, list[str]]] = field(default_factory=list)


def load_rule_arg(rule: str) -> Rule:
    """Rule name or path to a .json file."""
    p = Path(rule)
    return Rule.load(p if p.suffix == ".json" else RULES_DIR / f"{rule}.json")


async def _run_many(rule_for, urls, max_pages, concurrency, headless, say, respect_robots=True,
                    queries=None, max_queries=None, rule_terms=None):
    sem = asyncio.Semaphore(max(1, concurrency))
    need_browser = any(r and (needs_browser(r) or (r.detail is not None and r.detail.fetch == "browser"))
                       for r in (rule_for(u) for u in urls))

    async def one(u, session):
        rule = rule_for(u)
        if rule is None:
            say(f"[{u}] no matching rule, run 'learn' first")
            return u, None, None
        async with sem:
            say(f"[{u}] rule: {rule.name} ({rule.fetch}, {rule.pagination.type}"
                + (", search" if rule.search else "") + ")")
            try:
                r = await run_rule(rule, u, session, max_pages=max_pages,
                                   respect_robots=respect_robots, progress=lambda s: say(f"  [{u}] {s}"),
                                   queries=((list(dict.fromkeys([*(queries or []), *(rule_terms or {}).get(rule.name, [])]))
                                             or None) if rule.search else None),
                                   max_queries=max_queries)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # an error on one URL must not stop the others
                r = RunResult(url=u, errors=[f"run error: {type(e).__name__}: {e}"], stop_reason="error")
            return u, rule, r

    if need_browser:
        async with B.BrowserSession(headless=headless) as s:
            return await asyncio.gather(*(one(u, s) for u in urls))
    return await asyncio.gather(*(one(u, None) for u in urls))


async def run_urls(urls: list[str], *, rule: Optional[str] = None, max_pages: Optional[int] = None,
                   concurrency: int = 2, headless: bool = True, ignore_robots: bool = False,
                   out: Optional[Path] = None, say: Callable[[str], None] = print,
                   queries: Optional[list[str]] = None, max_queries: Optional[int] = None) -> RunOutcome:
    """queries / max_queries are used only by search rules (query limit 0 = unlimited).
    If a search rule is selected, no URL is needed: the rule's search page is used."""
    fixed = load_rule_arg(rule) if rule else None
    rule_for = (lambda u: fixed) if fixed else find_rule
    urls, rule_terms = _collapse_search_urls(list(urls), fixed, rule_for, say)
    if not urls:
        raise ValueError("No URL to run: the search rule's search page (search.page_url) is not set either.")
    if queries and not any(r and r.search for r in map(rule_for, urls)):
        say("Note: search terms were given but the rule is not a search rule; the terms were ignored.")
    requested, blocked = list(urls), set()  # blocked: indexes in input order
    if not ignore_robots:
        allowed = []
        for i, u in enumerate(requested):
            if await B.robots_allowed(u):
                allowed.append(u)
            else:
                blocked.add(i)
                say(f"[{u}] disallowed by robots.txt, skipped")
        urls = allowed
    results = iter(await _run_many(rule_for, urls, max_pages, concurrency, headless, say, not ignore_robots,
                                   queries, max_queries, rule_terms))

    payload = {"created": datetime.now().isoformat(timespec="seconds"), "results": []}
    rows, broken = [], []
    for idx, req in enumerate(requested):  # in input order; gather keeps the order
        if idx in blocked:
            name = getattr(rule_for(req), "name", None)
            payload["results"].append({"url": req, "rule": name, "status": "ROBOTS_BLOCKED", "stop_reason": "robots",
                                       "rounds": 0, "total_unique": 0, "issues": [],
                                       "notes": ["robots.txt disallows this page, skipped"], "items": []})
            rows.append({"url": req, "rule": name, "items": 0, "rounds": 0, "status": "ROBOTS_BLOCKED"})
            continue
        u, rl, r = next(results)
        if r is None:
            rows.append({"url": u, "rule": None, "items": 0, "rounds": 0, "status": "NO_RULE"})
            continue
        issues = check(rl, r, partial=False, limited=max_pages is not None and max_pages != 0)
        notes = soft_notes(rl, r)
        if issues:
            broken.append((u, rl.name, issues))
        for n in notes:
            say(f"[{u}] note: {n}")
        if r.stop_reason == "robots":
            status = "ROBOTS_BLOCKED"
        else:
            status = "PROBLEM" if issues else ("AT_LIMIT" if r.stop_reason in ("max_pages", "query_limit") else "OK")
        payload["results"].append({**r.summary(), "rule": rl.name, "status": status,
                                   "fill_rates": fill_rates(rl, r.items),
                                   "issues": issues, "notes": notes, "items": r.items})
        rows.append({"url": u, "rule": rl.name, "items": len(r.items), "rounds": r.rounds, "status": status})
    payload["total_items"] = sum(len(x["items"]) for x in payload["results"])
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    else:
        out = _write_unique(OUT_DIR, f"result-{datetime.now():%Y%m%d-%H%M%S}", text)
    return RunOutcome(payload=payload, out_path=out, rows=rows, broken=broken)


def _collapse_search_urls(urls, fixed, rule_for, say):
    """A search rule runs once, not once per URL: result URLs (…?q=python) become terms of that rule, and the
    search page and other URLs are collapsed into a single row so the same search is not repeated.
    Returns (urls, {rule name: terms taken from the URLs})."""
    from .rules import term_from_url
    if fixed is not None and fixed.search is not None:
        s = fixed.search
        home = s.page_url or fixed.meta.get("sample_url")
        if not home and s.url_template:
            from urllib.parse import urlparse
            p = urlparse(s.url_template)
            home = f"{p.scheme}://{p.netloc}/"
        terms = [t for t in (term_from_url(s, u) for u in urls) if t]
        other = [u for u in urls if term_from_url(s, u) is None and u.rstrip("/") != (home or "").rstrip("/")]
        if other:
            say(f"Note: a search rule runs once, not once per URL; {len(other)} URL(s) ignored.")
        return ([home] if home else []), ({fixed.name: list(dict.fromkeys(terms))} if terms else {})
    out, seen_rules, rule_terms = [], {}, {}
    for u in urls:
        r = rule_for(u)
        if r is None or r.search is None:
            out.append(u)
            continue
        t = term_from_url(r.search, u)
        if t:
            say(f"[{u}] search result URL: will search for '{t}' ({r.name})")
            rule_terms.setdefault(r.name, [])
            if t not in rule_terms[r.name]:
                rule_terms[r.name].append(t)
        if r.name in seen_rules:
            continue
        seen_rules[r.name] = u
        out.append(r.search.page_url or u)
    return out, rule_terms


def _write_unique(folder: Path, stem: str, text: str) -> Path:
    """Jobs finishing in the same second must not overwrite each other's file: open in 'x' mode, add -2, -3… if taken."""
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(1, 1000):
        p = folder / (f"{stem}.json" if i == 1 else f"{stem}-{i}.json")
        try:
            with p.open("x", encoding="utf-8") as f:
                f.write(text)
            return p
        except FileExistsError:
            continue
    raise FileExistsError(f"{folder / stem}*.json")
