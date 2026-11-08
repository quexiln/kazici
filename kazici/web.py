"""Web UI: Starlette server + background jobs (learn, run, simplify).

Scraping jobs do not run on the server's event loop but on a single event loop in a separate thread.
This guarantees a Proactor loop on Windows for the Playwright/SDK child processes, and browser.throttle's
per-host locks are shared by all jobs on the same loop.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import browser as B
from .llm import DEFAULT_MODEL
from .rules import RULES_DIR, Rule, all_rules, find_rule
from .service import OUT_DIR, run_urls

STATIC = Path(__file__).resolve().parent / "static"
SAFE_NAME = re.compile(r"^[\w.\-]+$")
MAX_LOG = 5000

# ---------------------------------------------------------------- job loop

_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_lock = threading.Lock()


def _worker_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, name="kazici-jobs", daemon=True).start()
        return _loop


@dataclass
class Job:
    id: str
    kind: str                       # learn | run | simplify
    title: str
    params: dict
    status: str = "running"         # running | done | error | cancelled
    log: list[str] = field(default_factory=list)
    log_offset: int = 0             # lines dropped from the start; ?since= is an absolute line number
    result: Any = None
    error: str = ""
    started: float = field(default_factory=time.time)
    finished: Optional[float] = None
    future: Any = None

    def say(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")
        if len(self.log) > MAX_LOG:
            drop = len(self.log) - MAX_LOG
            del self.log[:drop]
            self.log_offset += drop

    def brief(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
                "started": self.started, "finished": self.finished, "error": self.error,
                "log_len": self.log_offset + len(self.log), "params": self.params}


JOBS: dict[str, Job] = {}


def _start_job(kind: str, title: str, params: dict, make_coro) -> Job:
    job = Job(id=uuid.uuid4().hex[:10], kind=kind, title=title, params=params)
    JOBS[job.id] = job

    async def wrapper():
        try:
            job.result = await make_coro(job)
            job.status = "done"
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.say("Job cancelled.")
        except Exception as e:  # noqa: BLE001 — show the error to the user
            job.status, job.error = "error", f"{type(e).__name__}: {e}"
            job.say(f"ERROR: {job.error}")
        finally:
            job.finished = time.time()

    job.future = asyncio.run_coroutine_threadsafe(wrapper(), _worker_loop())
    return job


# ---------------------------------------------------------------- jobs

async def _learn_job(job: Job) -> dict:
    from .learner import learn

    p = job.params
    if not p["ignore_robots"] and not await B.robots_allowed(p["url"]):
        raise RuntimeError("robots.txt disallows this page (you can bypass it with the 'Ignore robots.txt' option)")
    res = await learn(p["url"], p["goal"], name=p["name"] or None, also=p["also"], max_iter=p["max_iter"],
                      test_pages=p["test_pages"], model=p["model"], headless=not p["show_browser"], say=job.say,
                      search=p.get("search", "auto"))
    if res.rule is None:
        raise RuntimeError(res.failure or "Could not learn a rule.")
    path = res.rule.save()
    job.say(f"{'VERIFIED' if res.accepted else 'UNVERIFIED (please check)'} — rule saved: {path.name}")
    return {"rule": res.rule.name, "accepted": res.accepted, "llm_calls": res.llm_calls,
            "attempts": len(res.attempts), "cost_usd": round(res.cost_usd, 4),
            "url_pattern": res.rule.url_pattern, "fetch": res.rule.fetch,
            "pagination": res.rule.pagination.type, "search": _search_brief(res.rule)}


async def _run_job(job: Job) -> dict:
    p = job.params
    out = None
    if p.get("out"):
        out = OUT_DIR / _safe_json_name(p["out"])
    res = await run_urls(p["urls"], rule=p["rule"] or None, max_pages=p["max_pages"],
                         concurrency=p["concurrency"], headless=not p["show_browser"],
                         ignore_robots=p["ignore_robots"], out=out, say=job.say,
                         queries=p.get("queries") or None, max_queries=p.get("max_queries"))
    job.say(f"Total {res.payload['total_items']} items → {res.out_path.name}")
    return {"output": res.out_path.name, "total_items": res.payload["total_items"], "rows": res.rows,
            "broken": [{"url": u, "rule": n, "issues": i} for u, n, i in res.broken]}


async def _simplify_job(job: Job) -> dict:
    from .learner import snapshot_page

    async with B.BrowserSession() as s:
        sp = await snapshot_page(s, job.params["url"])
    text = sp.as_prompt()
    job.say(f"Simplified: {sp.raw_chars:,} → {len(text):,} characters")
    return {"text": text, "raw_chars": sp.raw_chars, "chars": len(text)}


# ---------------------------------------------------------------- helpers

def _err(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=code)


def _safe_json_name(name: str) -> str:
    name = Path(name).name
    if not name.endswith(".json"):
        name += ".json"
    if not SAFE_NAME.match(name):
        raise ValueError("invalid file name")
    return name


def _is_url(u: str) -> bool:
    return bool(re.match(r"^https?://\S+$", u or ""))


def _int(v, default, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _search_brief(r: Rule):
    s = r.search
    if s is None:
        return None
    return {"how": s.how, "terms": s.terms, "queries": s.queries, "method": s.enum.method if s.enum else None,
            "max_queries": s.max_queries, "page_url": s.page_url}


def _rule_brief(path: Path, r: Rule) -> dict:
    return {"name": r.name, "file": path.name, "site": r.site, "goal": r.goal, "fetch": r.fetch,
            "search": _search_brief(r),
            "pagination": r.pagination.type, "url_pattern": r.url_pattern, "fields": [f.name for f in r.fields],
            "verified": bool(r.meta.get("verified")), "learned_at": r.meta.get("learned_at"),
            "sample_url": r.meta.get("sample_url"), "mtime": path.stat().st_mtime}


_out_cache: dict[str, tuple[float, dict]] = {}


def _output_brief(path: Path) -> dict:
    st = path.stat()
    cached = _out_cache.get(path.name)
    if cached and cached[0] == st.st_mtime:
        return cached[1]
    info = {"name": path.name, "size": st.st_size, "mtime": st.st_mtime, "total_items": None,
            "urls": [], "rules": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        res = data.get("results", [])
        info.update(total_items=data.get("total_items", sum(len(x.get("items", [])) for x in res)),
                    urls=[x.get("url") for x in res][:5], rules=sorted({x.get("rule") for x in res if x.get("rule")}),
                    created=data.get("created"),
                    statuses=[x.get("status") or ("PROBLEM" if x.get("issues") else "OK") for x in res])
    except Exception as e:  # noqa: BLE001
        info["error"] = f"unreadable: {type(e).__name__}"
    _out_cache[path.name] = (st.st_mtime, info)
    return info


# ---------------------------------------------------------------- endpoints

async def index(_: Request):
    # Add a version to static files so the browser does not use a cached old app.js after a code change
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for f in ("app.js", "app.css"):
        html = html.replace(f"/static/{f}", f"/static/{f}?v={int((STATIC / f).stat().st_mtime)}")
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


async def api_rules(_: Request):
    rows, warns = [], []
    for p, r in all_rules(warn=warns.append):
        rows.append(_rule_brief(p, r))
    rows.sort(key=lambda x: x["mtime"], reverse=True)
    return JSONResponse({"rules": rows, "warnings": warns})


async def api_rule(req: Request):
    name = req.path_params["name"]
    if not SAFE_NAME.match(name):
        return _err("invalid name")
    path = RULES_DIR / f"{name}.json"
    if req.method == "GET":
        if not path.exists():
            return _err("no such rule", 404)
        return Response(path.read_text(encoding="utf-8-sig"), media_type="application/json")
    if req.method == "DELETE":
        if not path.exists():
            return _err("no such rule", 404)
        path.unlink()
        return JSONResponse({"ok": True})
    # PUT: save (rename if the name changed)
    try:
        body = await req.json()
        rule = Rule.model_validate(body)
    except ValidationError as e:
        return _err("The rule does not match the schema:\n" + "\n".join(
            f"• {'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()))
    except Exception as e:  # noqa: BLE001
        return _err(f"Invalid JSON: {e}")
    if not SAFE_NAME.match(rule.name):
        return _err("A rule name may contain only letters, digits, '-', '_' and '.'")
    try:
        re.compile(rule.url_pattern)
    except re.error as e:
        return _err(f"url_pattern is not a valid regex: {e}")
    renamed = rule.name != name
    target = RULES_DIR / f"{rule.name}.json"
    case_only = renamed and target.exists() and path.exists() and target.samefile(path)  # only the letter case changed
    clash = renamed and target.exists() and not case_only
    if clash and req.query_params.get("overwrite") not in ("1", "true"):
        return _err(f"A rule named '{rule.name}' already exists. Confirm to overwrite it.", 409)
    if case_only:
        path.rename(target)  # so the file name's letter case changes on Windows too
    new_path = rule.save()
    if renamed and not case_only and new_path != path and path.exists():
        path.unlink()
    return JSONResponse({"ok": True, "name": rule.name})


async def api_match(req: Request):
    url = req.query_params.get("url", "")
    if not _is_url(url):
        return JSONResponse({"rule": None})
    r = find_rule(url)
    return JSONResponse({"rule": r.name if r else None,
                         "fetch": r.fetch if r else None, "pagination": r.pagination.type if r else None})


async def api_learn(req: Request):
    b = await req.json()
    url, goal = (b.get("url") or "").strip(), (b.get("goal") or "").strip()
    if not _is_url(url):
        return _err("Enter a valid URL (http/https).")
    if not goal:
        return _err("Describe the data you want.")
    name = (b.get("name") or "").strip()
    if name and not SAFE_NAME.match(name):
        return _err("A rule name may contain only letters, digits, '-', '_' and '.'")
    also = [u.strip() for u in (b.get("also") or []) if u.strip()]
    bad = [u for u in also if not _is_url(u)]
    if bad:
        return _err(f"Invalid extra URL: {bad[0]}")
    params = {"url": url, "goal": goal, "name": name, "also": also,
              "max_iter": _int(b.get("max_iter"), 8, 1, 12), "test_pages": _int(b.get("test_pages"), 3, 1, 10),
              "model": b.get("model") or DEFAULT_MODEL, "show_browser": bool(b.get("show_browser")),
              "ignore_robots": bool(b.get("ignore_robots")),
              "search": b.get("search") if b.get("search") in ("auto", "on", "off") else "auto"}
    job = _start_job("learn", f"Learn: {goal[:60]}", params, _learn_job)
    return JSONResponse(job.brief())


async def api_run(req: Request):
    b = await req.json()
    for key in ("urls", "queries"):
        v = b.get(key)
        if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) for x in v)):
            return _err(f"'{key}' must be a list of strings.")
    urls = [u.strip() for u in (b.get("urls") or []) if u.strip() and not u.strip().startswith("#")]
    queries = [q.strip() for q in (b.get("queries") or []) if q.strip() and not q.strip().startswith("#")]
    rule = (b.get("rule") or "").strip() if isinstance(b.get("rule"), str) else ""
    if rule and not SAFE_NAME.match(rule):
        return _err("A rule name may contain only letters, digits, '-', '_' and '.'")
    if rule and not (RULES_DIR / f"{rule}.json").exists():
        return _err(f"Rule not found: {rule}")
    if not urls:
        try:
            sr = Rule.load(RULES_DIR / f"{rule}.json") if rule else None
        except Exception as e:
            return _err(f"Could not read the rule: {str(e).splitlines()[0] if str(e) else type(e).__name__}")
        if sr is None or sr.search is None:
            return _err("Enter at least one URL (not needed if a search rule is selected).")
    bad = [u for u in urls if not _is_url(u)]
    if bad:
        return _err(f"Invalid URL: {bad[0]}")
    if not rule:
        missing = [u for u in urls if find_rule(u) is None]
        if len(missing) == len(urls):
            return _err("No matching rule for these URLs. Create one with 'Learn' first, or pick a rule.")
    out = (b.get("out") or "").strip()
    if out:
        try:
            _safe_json_name(out)
        except ValueError:
            return _err("An output name may contain only letters, digits, '-', '_' and '.'")
    # max_pages: "default"/empty = the rule's limit, "all" = all pages (0), number = custom limit (no upper bound)
    mp = b.get("max_pages")
    if mp in (None, "", "default"):
        mp = None
    elif mp == "all":
        mp = 0
    else:
        try:
            mp = int(mp)
        except (TypeError, ValueError):
            return _err("The page limit must be a number.")
        if mp < 1:
            return _err("A custom page limit must be at least 1.")
    mq = b.get("max_queries")
    if mq in (None, "", "default"):
        mq = None
    elif mq == "all":
        mq = 0
    else:
        try:
            mq = int(mq)
        except (TypeError, ValueError):
            return _err("The query limit must be a number.")
        if mq < 1:
            return _err("A custom query limit must be at least 1.")
    params = {"urls": urls, "rule": rule, "max_pages": mp, "queries": queries, "max_queries": mq,
              "concurrency": _int(b.get("concurrency"), 2, 1, 8), "show_browser": bool(b.get("show_browser")),
              "ignore_robots": bool(b.get("ignore_robots")), "out": out}
    title = (f"Run: {urls[0][:60]}" + (f" (+{len(urls) - 1})" if len(urls) > 1 else "")) if urls         else f"Search: {rule}" + (f" ({', '.join(queries)[:50]})" if queries else "")
    job = _start_job("run", title, params, _run_job)
    return JSONResponse(job.brief())


async def api_simplify(req: Request):
    b = await req.json()
    url = (b.get("url") or "").strip()
    if not _is_url(url):
        return _err("Enter a valid URL (http/https).")
    job = _start_job("simplify", f"Simplify: {url[:60]}", {"url": url}, _simplify_job)
    return JSONResponse(job.brief())


async def api_jobs(_: Request):
    jobs = sorted(JOBS.values(), key=lambda j: j.started, reverse=True)
    return JSONResponse({"jobs": [j.brief() for j in jobs]})


async def api_job(req: Request):
    job = JOBS.get(req.path_params["id"])
    if not job:
        return _err("no such job", 404)
    since = _int(req.query_params.get("since"), 0, 0, 10 ** 9)
    start = max(since, job.log_offset)  # if dropped lines were requested, start from the first line still held
    return JSONResponse({**job.brief(), "log": job.log[start - job.log_offset:], "log_from": start,
                         "result": job.result if job.status != "running" else None})


async def api_job_cancel(req: Request):
    job = JOBS.get(req.path_params["id"])
    if not job:
        return _err("no such job", 404)
    if job.status == "running" and job.future:
        job.future.cancel()
        job.say("Cancellation requested…")
    return JSONResponse(job.brief())


_server = None  # the uvicorn.Server created in serve()


async def api_shutdown(_: Request):
    """Shuts the server down. Running jobs are cancelled (browsers close), then the server stops."""
    running = [j for j in JOBS.values() if j.status == "running" and j.future]
    for j in running:
        j.future.cancel()
        j.say("Server is stopping, job cancelled.")

    def stop():
        deadline = time.time() + 10  # give cancelled jobs time to close their browsers
        while time.time() < deadline and any(j.status == "running" for j in running):
            time.sleep(0.2)
        if _server is not None:
            _server.should_exit = True

    threading.Thread(target=stop, daemon=True).start()
    return JSONResponse({"ok": True, "cancelled": len(running)})


async def api_outputs(_: Request):
    OUT_DIR.mkdir(exist_ok=True)
    files = sorted(OUT_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return JSONResponse({"outputs": [_output_brief(p) for p in files]})


async def api_output(req: Request):
    name = req.path_params["name"]
    if not SAFE_NAME.match(name) or not name.endswith(".json"):
        return _err("invalid name")
    path = OUT_DIR / name
    if not path.exists():
        return _err("no such file", 404)
    if req.method == "DELETE":
        path.unlink()
        _out_cache.pop(name, None)
        return JSONResponse({"ok": True})
    if req.query_params.get("download"):
        return FileResponse(path, media_type="application/json", filename=name)
    return FileResponse(path, media_type="application/json")


# ---------------------------------------------------------------- local access guard
# The server is only used from this machine. A page on another site must not send requests through the browser
# (CSRF), and foreign Host names from DNS rebinding are rejected. Same-origin app.js and clients that send no
# Origin (kazici_launcher.pyw, curl) are not affected.

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
_extra_hosts: set[str] = set()  # address given explicitly via serve(--host)


def _host_name(hostport: str) -> str:
    hostport = (hostport or "").strip().lower()
    if hostport.startswith("["):
        return hostport[: hostport.find("]") + 1] if "]" in hostport else hostport
    return hostport.rsplit(":", 1)[0] if hostport.count(":") == 1 else hostport


class LocalOnly:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        allowed = LOCAL_HOSTS | _extra_hosts
        host = headers.get("host", "")
        if _host_name(host) not in allowed:
            return await _err("This server can only be reached at 127.0.0.1/localhost.", 403)(
                scope, receive, send)
        origin = headers.get("origin")
        if scope["method"] not in ("GET", "HEAD", "OPTIONS") and origin is not None:
            m = re.match(r"^https?://([^/]+)$", origin.strip().lower())
            if not m or m.group(1) != host.strip().lower():
                return await _err("Request from another site rejected.", 403)(scope, receive, send)
        return await self.app(scope, receive, send)


app = Starlette(routes=[
    Route("/", index),
    Route("/api/rules", api_rules),
    Route("/api/rules/{name}", api_rule, methods=["GET", "PUT", "DELETE"]),
    Route("/api/match", api_match),
    Route("/api/learn", api_learn, methods=["POST"]),
    Route("/api/run", api_run, methods=["POST"]),
    Route("/api/simplify", api_simplify, methods=["POST"]),
    Route("/api/jobs", api_jobs),
    Route("/api/jobs/{id}", api_job),
    Route("/api/jobs/{id}/cancel", api_job_cancel, methods=["POST"]),
    Route("/api/shutdown", api_shutdown, methods=["POST"]),
    Route("/api/outputs", api_outputs),
    Route("/api/outputs/{name}", api_output, methods=["GET", "DELETE"]),
    Mount("/static", StaticFiles(directory=STATIC), name="static"),
])
app.add_middleware(LocalOnly)


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    global _server
    import uvicorn

    _worker_loop()
    if host not in ("0.0.0.0", "::"):
        _extra_hosts.add(_host_name(f"[{host}]" if ":" in host else host))
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
    print(f"kazici web UI: {url}  (to stop: Ctrl+C or 'Stop server' in the UI)", flush=True)
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    _server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    _server.run()
