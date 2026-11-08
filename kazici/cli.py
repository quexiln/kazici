"""Command line: learn (learn a rule with the LLM), run (run a rule without the LLM), rules, show, simplify, web."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import browser as B
from .llm import DEFAULT_MODEL
from .rules import RULES_DIR, all_rules
from .service import run_urls

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

app = typer.Typer(add_completion=False, help="Learn a site-specific extraction rule with an LLM, then run it without the LLM.")
con = Console()


def _say(msg: str) -> None:
    con.print(msg, highlight=False, markup=False)


@app.command()
def learn(
    url: str = typer.Argument(..., help="Page that contains the data"),
    goal: str = typer.Argument(..., help="Data you want, e.g. 'all product links, name and price'"),
    name: Optional[str] = typer.Option(None, help="Rule name (file name)"),
    also: List[str] = typer.Option([], "--also", help="Other sample page(s) the rule must also work on"),
    max_iter: int = typer.Option(8, help="Maximum number of LLM calls"),
    test_pages: int = typer.Option(3, help="Number of pages to test while learning"),
    model: str = typer.Option(DEFAULT_MODEL, help="Initial model"),
    show_browser: bool = typer.Option(False, "--show-browser", help="Show the browser window"),
    ignore_robots: bool = typer.Option(False, help="Ignore robots.txt"),
    search: Optional[bool] = typer.Option(
        None, "--search/--no-search",
        help="Search: --search forces it, --no-search disables it; if omitted, search is used automatically when needed"),
):
    """Looks at the page, learns a rule by trial and error and saves it to the rules/ folder.
    If the requested records are found through a search (e.g. 'videos titled X'), the site's search is learned."""
    from .learner import learn as _learn

    if not ignore_robots and not asyncio.run(B.robots_allowed(url)):
        con.print("[red]robots.txt disallows this page.[/] (use --ignore-robots to override)")
        raise typer.Exit(2)
    res = asyncio.run(_learn(url, goal, name=name, also=also, max_iter=max_iter, test_pages=test_pages,
                             model=model, headless=not show_browser, say=_say,
                             search="auto" if search is None else ("on" if search else "off")))
    if res.rule is None:
        con.print("[red]Could not learn a rule.[/] " + escape(res.failure))
        raise typer.Exit(1)
    path = res.rule.save()
    status = "[green]VERIFIED[/]" if res.accepted else "[yellow]UNVERIFIED (check it)[/]"
    con.print(f"\n{status} rule saved: {escape(str(path))}")
    con.print(f"LLM calls: {res.llm_calls}, attempts: {len(res.attempts)}, "
              f"estimated cost: ${res.cost_usd:.4f} (informational on a subscription)")
    if res.rule.search is not None:
        _say(f"To run it: python -m kazici run --rule {res.rule.name}"
             + (" --query \"another term\"" if res.rule.search.terms == "given" else ""))
    else:
        _say(f"To run it: python -m kazici run \"{url}\" --rule {res.rule.name}")
    _say(f"This rule is also used automatically on pages matching this URL pattern: {res.rule.url_pattern}")


@app.command()
def run(
    urls: List[str] = typer.Argument(None, help="One or more pages"),
    file: Optional[Path] = typer.Option(None, "--file", "-f", help="File with one URL per line"),
    rule: Optional[str] = typer.Option(None, "--rule", "-r", help="Rule name or path (if omitted, found from the URL)"),
    max_pages: Optional[int] = typer.Option(None, help="Page limit (default: the rule's, 0 = all)"),
    all_pages: bool = typer.Option(False, "--all", help="No page limit: continue until the list ends"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Output JSON file"),
    concurrency: int = typer.Option(2, help="Number of URLs processed at the same time"),
    show_browser: bool = typer.Option(False, "--show-browser"),
    ignore_robots: bool = typer.Option(False),
    query: List[str] = typer.Option([], "--query", "-q", help="Search rule: term to search for (can be given multiple times)"),
    queries_file: Optional[Path] = typer.Option(None, "--queries", help="Search rule: file with one term per line"),
    max_queries: Optional[int] = typer.Option(None, help="Search rule: maximum number of queries (default: the rule's, 0 = unlimited)"),
):
    """Runs a saved rule on one or more pages without the LLM. A search rule needs no URL
    (--rule is enough); if no terms are given, the rule's own terms or its enumeration method are used."""
    all_urls = list(urls or [])
    if file:
        all_urls += [s for s in (l.strip() for l in file.read_text(encoding="utf-8-sig").splitlines())
                     if s and not s.startswith("#")]
    terms = list(query)
    if queries_file:
        terms += [s for s in (l.strip() for l in queries_file.read_text(encoding="utf-8-sig").splitlines())
                  if s and not s.startswith("#")]
    if not all_urls and not rule:
        con.print("[red]Give a URL (argument or --file), or --rule for a search rule.[/]")
        raise typer.Exit(2)
    if not all_urls:
        from .service import load_rule_arg
        try:
            is_search = load_rule_arg(rule).search is not None
        except Exception as e:
            con.print(f"[red]Could not read the rule:[/] {escape(str(e).splitlines()[0])}")
            raise typer.Exit(2)
        if not is_search:
            con.print("[red]This rule is not a search rule; give a URL.[/]")
            raise typer.Exit(2)
    if max_queries is not None and max_queries < 0:
        con.print("[red]--max-queries must be 0 or positive.[/]")
        raise typer.Exit(2)
    if all_pages:
        max_pages = 0
    if max_pages is not None and max_pages < 0:
        con.print("[red]--max-pages must be 0 or positive.[/]")
        raise typer.Exit(2)
    res = asyncio.run(run_urls(all_urls, rule=rule, max_pages=max_pages,
                               concurrency=concurrency,
                               headless=not show_browser, ignore_robots=ignore_robots, out=out, say=_say,
                               queries=terms or None, max_queries=max_queries))
    table = Table("URL", "Rule", "Unique items", "Pages/rounds", "Status")
    for row in res.rows:
        table.add_row(escape(row["url"][:70]), escape(row["rule"] or "-"), str(row["items"]), str(row["rounds"]),
                      row["status"].lower() if row["rule"] is None else row["status"])
    con.print(table)
    for u, name, issues in res.broken:
        con.print(f"[yellow]{escape(u)}[/] ({escape(name)}):")
        for i in issues:
            con.print(f"   - {i}", markup=False)
    if res.broken:
        con.print("[yellow]The rule may be broken (the site may have changed). To learn it again: "
                  "python -m kazici learn URL \"goal\" --name RULE_NAME[/]")
    con.print(f"Total {res.payload['total_items']} items → {escape(str(res.out_path))}")


@app.command("rules")
def list_rules():
    """Lists saved rules."""
    t = Table("Name", "Site", "Goal", "Fetch", "Pagination", "Search", "Verified")
    for p, r in all_rules(warn=lambda m: con.print(f"[yellow]{m}[/]")):
        srch = "-" if r.search is None else (", ".join(r.search.queries)[:20] if r.search.terms == "given"
                                             else f"all ({r.search.enum.method if r.search.enum else '?'})")
        t.add_row(escape(r.name), escape(r.site), escape(r.goal[:40]), r.fetch, r.pagination.type, escape(srch),
                  "yes" if r.meta.get("verified") else "no")
    con.print(t)


@app.command()
def show(name: str):
    """Shows a rule's JSON."""
    con.print_json((RULES_DIR / f"{name}.json").read_text(encoding="utf-8-sig"))


@app.command("simplify")
def simplify_cmd(url: str, out: Optional[Path] = typer.Option(None, "--out", "-o")):
    """Shows the simplified page view that is sent to the LLM (for debugging)."""
    from .learner import snapshot_page

    async def go():
        async with B.BrowserSession() as s:
            return await snapshot_page(s, url)
    sp = asyncio.run(go())
    text = sp.as_prompt()
    if out:
        out.write_text(text, encoding="utf-8")
        con.print(f"{len(text):,} characters → {out}")
    else:
        con.print(text, markup=False, highlight=False)


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", help="Address to listen on"),
    port: int = typer.Option(8765, help="Port"),
    no_open: bool = typer.Option(False, "--no-open", help="Do not open the browser automatically"),
):
    """Starts the web interface (learn, run, rules, results)."""
    from .web import serve
    serve(host=host, port=port, open_browser=not no_open)


if __name__ == "__main__":
    app()
