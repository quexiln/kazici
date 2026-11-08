"""Opening pages with Playwright (async), setup steps and robots.txt checks."""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

import httpx
from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from protego import Protego
from w3lib.encoding import html_to_unicode

from .rules import Sel, Step

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/140.0 Safari/537.36")
BLOCK_TYPES = {"image", "media", "font"}


def pw_selector(sel: Sel) -> str:
    return ("xpath=" if sel.kind == "xpath" else "css=") + sel.value


class BrowserSession:
    """A single Chromium instance; a new tab for each URL."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._pw = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None

    async def __aenter__(self) -> "BrowserSession":
        try:
            self._pw = await async_playwright().start()
            self.browser = await self._pw.chromium.launch(headless=self.headless)
            self.context = await self.browser.new_context(
                user_agent=UA, locale="tr-TR", viewport={"width": 1366, "height": 900})

            async def _route(route):
                if route.request.resource_type in BLOCK_TYPES:
                    await route.abort()
                else:
                    await route.continue_()

            await self.context.route("**/*", _route)
        except BaseException:
            try:
                await self.__aexit__(None, None, None)  # do not leave the Playwright driver/browser running
            except Exception:
                pass
            raise
        return self

    async def __aexit__(self, *exc):
        try:
            if self.browser:
                await self.browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def new_page(self) -> Page:
        assert self.context is not None
        page = await self.context.new_page()
        page.set_default_timeout(15000)
        return page


MAX_CRAWL_DELAY_MS = 60000
_host_delay: dict[str, int] = {}
_host_last: dict[str, float] = {}
_host_lock: dict[str, asyncio.Lock] = {}


def _host(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


def set_host_delay(url: str, ms: int) -> None:
    """Sets the host's delay. If the host received a request in the last 2 minutes (another run may be in
    progress) the larger value is kept; if the host has been idle the old value does not linger, the new one applies."""
    h = _host(url)
    ms = max(0, int(ms))
    if time.monotonic() - _host_last.get(h, float("-inf")) < 120:
        ms = max(ms, _host_delay.get(h, 0))
    _host_delay[h] = ms


async def throttle(url: str) -> None:
    """Queue requests to the same site (page loads, HTTP) at least the delay apart.
    Multiple concurrent runs and learning trials follow this queue too."""
    h = _host(url)
    lock = _host_lock.setdefault(h, asyncio.Lock())
    async with lock:
        delay = max(_host_delay.get(h, 0), await robots_delay_ms(url)) / 1000
        wait = _host_last.get(h, 0) + delay - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _host_last[h] = time.monotonic()


async def goto(page: Page, url: str, settle_ms: int = 1500) -> None:
    await throttle(url)
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    try:
        await page.wait_for_load_state("load", timeout=10000)
    except Exception:
        pass
    await page.wait_for_timeout(settle_ms)


async def run_setup(page: Page, steps: list[Step]) -> list[str]:
    """Runs the setup steps and returns the errors as a list."""
    errors = []
    for i, st in enumerate(steps, 1):
        try:
            if st.action == "wait":
                await page.wait_for_timeout(st.ms)
            elif st.action == "press_escape":
                await page.keyboard.press("Escape")
            elif st.action == "scroll_bottom":
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(st.ms)
            elif st.action == "wait_for" and st.selector:
                await page.locator(pw_selector(st.selector)).first.wait_for(
                    timeout=3000 if st.optional else 15000)
            elif st.action == "click" and st.selector:
                loc = page.locator(pw_selector(st.selector)).first
                await loc.wait_for(state="visible", timeout=2500 if st.optional else 10000)
                await loc.click(timeout=5000)
                await page.wait_for_timeout(min(st.ms, 3000))
        except Exception as e:
            if not st.optional:
                errors.append(f"setup step {i} ({st.action} {st.selector.value if st.selector else ''}): "
                              f"{type(e).__name__}: {(str(e).splitlines() or [''])[0][:200]}")
    return errors


async def gentle_scroll(page: Page, rounds: int = 3) -> None:
    """Gradual scrolling to trigger lazy-loaded content."""
    for _ in range(rounds):
        await page.mouse.wheel(0, 2500)
        await page.wait_for_timeout(500)
    await page.evaluate("window.scrollTo(0, 0)")


# ---------------------------------------------------------------- robots.txt

ROBOTS_TTL = 3600       # 200/4xx response: refreshed now and then because the web server is long-lived
ROBOTS_RETRY_TTL = 60   # network error, 5xx, 429: not kept for long, asked again shortly
_robots_cache: dict[str, tuple[float, Protego | None]] = {}   # base -> (expiry time, rules; None = allowed)
_DISALLOW_ALL = "User-agent: *\nDisallow: /\n"


async def _robots(url: str) -> Protego | None:
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    hit = _robots_cache.get(base)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    ttl = ROBOTS_TTL
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers={"User-Agent": UA}) as c:
            r = await c.get(base + "/robots.txt")
        if r.status_code == 200:
            rp = Protego.parse(r.text)
        elif r.status_code >= 500 or r.status_code == 429:
            rp, ttl = Protego.parse(_DISALLOW_ALL), ROBOTS_RETRY_TTL  # RFC 9309: server error = everything disallowed
        else:
            rp = None  # 4xx (no robots.txt): allowed
    except Exception:
        rp, ttl = None, ROBOTS_RETRY_TTL  # network error: allowed this time, retry shortly
    _robots_cache[base] = (time.monotonic() + ttl, rp)
    return rp


async def robots_allowed(url: str) -> bool:
    rp = await _robots(url)
    return True if rp is None else rp.can_fetch(url, "*")


async def robots_delay_ms(url: str) -> int:
    """robots.txt Crawl-delay (ms); 0 if none."""
    rp = await _robots(url)
    try:
        d = rp.crawl_delay("*") if rp else None
    except Exception:
        d = None
    return min(int(float(d) * 1000), MAX_CRAWL_DELAY_MS) if d else 0


async def http_get(client: httpx.AsyncClient, url: str) -> tuple[str, str]:
    """(html text, URL after redirects). The encoding is detected, including <meta charset>."""
    await throttle(url)
    r = await client.get(url)
    r.raise_for_status()
    _enc, text = html_to_unicode(r.headers.get("content-type"), r.content)
    return text, str(r.url)


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30, follow_redirects=True, headers={
        "User-Agent": UA, "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})


async def sleep_ms(ms: int) -> None:
    await asyncio.sleep(max(0, ms) / 1000)
