"""Simplify the page for the LLM.

1. Drop unneeded tags, hidden elements and attributes that selectors do not need.
2. Detect repeated sibling elements (product cards etc.): these are region candidates.
3. In the DOM view, reduce repeats to 2 samples and summarise the rest in a one-line comment.
4. List pagination / "load more" / cookie buttons and count hints like "1,234 products" separately.
"""
from __future__ import annotations

import copy
import re
from collections import defaultdict
from dataclasses import dataclass, field

import lxml.html
from lxml import etree

DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "template", "link", "meta",
             "canvas", "video", "audio", "source", "object", "embed")
KEEP_ATTRS = {"id", "class", "href", "src", "alt", "title", "aria-label", "role", "type",
              "name", "rel", "itemprop", "itemtype", "value", "disabled", "aria-disabled", "data-src"}
IDENT = re.compile(r"^-?[_a-zA-Z][_a-zA-Z0-9-]*$")
HIDDEN_ATTR = "data-kh"

NEXT_RE = re.compile(r"^\s*(?:(?:sonraki|next|ileri)(?:\s+(?:sayfa|page))?\s*[›»>→]*|[›»>→]+)\s*$", re.I)
MORE_RE = re.compile(r"(daha fazla|load more|show more|see more|more results|devam[ıi]|tümünü göster|"
                     r"daha fazla göster|daha fazla ürün|load next)", re.I)
COOKIE_RE = re.compile(r"(çerez|cookie|kabul|accept|reddet|reject|sadece gerekli|necessary only|"
                       r"agree|onayla|tamam|got it|anladım|kapat|close)", re.I)
COUNT_RE = re.compile(r"(\d{1,3}(?:[.,]\d{3})+|\d+)\s*\+?\s*(ürün|sonuç|kayıt|ilan|adet|kitap|results?|items?|"
                      r"products?|listings?|matches)", re.I)

# Runs in the browser: marks invisible elements (the topmost hidden ancestor).
MARK_HIDDEN_JS = """
() => {
  let n = 0;
  for (const el of document.body ? document.body.querySelectorAll('*') : []) {
    try {
      if (el.parentElement && el.parentElement.hasAttribute('data-kh')) continue;
      if (getComputedStyle(el).display === 'contents') continue;
      if (el.checkVisibility && !el.checkVisibility({checkVisibilityCSS: true})) {
        el.setAttribute('data-kh', '1'); n++;
      }
    } catch (e) {}
  }
  return n;
}
"""


@dataclass
class Region:
    selector: str
    count: int
    short_selector: str
    short_count: int
    distinct_links: int
    sample_html: str
    sample_text: str


@dataclass
class Control:
    kind: str  # next | more | cookie | pager
    selector: str
    text: str
    href: str = ""
    count: int = 1
    xpath: str = ""
    xpath_count: int = 0


@dataclass
class SearchField:
    """A search box on the page, or a select (dropdown) candidate in a search form."""
    index: int
    kind: str              # text | select
    selector: str          # css matching a single element on the page
    name: str = ""
    label: str = ""        # placeholder / aria-label / title
    form_action: str = ""
    form_method: str = "get"
    submit: str = ""       # the form's submit button (unique css), empty if none
    options: list[tuple[str, str]] = field(default_factory=list)  # select: (value, label)
    hidden: bool = False

    def line(self) -> str:
        s = f"{self.index}. [{self.kind}] css: {self.selector}"
        if self.name:
            s += f" name={self.name}"
        if self.label:
            s += f" label='{self.label}'"
        if self.form_action or self.form_method != "get":
            s += f" | form {self.form_method.upper()} {self.form_action or '(same page)'}"
        if self.submit:
            s += f" | button: {self.submit}"
        if self.options:
            s += f" | {len(self.options)} options: " + ", ".join(l for _, l in self.options[:8])
        if self.hidden:
            s += " | (currently hidden)"
        return s


SEARCH_NAME_RE = re.compile(r"^(q|qs|query|search|search_?query|s|k|kw|keywords?|term|ara|arama|aranan|"
                            r"kelime|filter_name|searchterm|search_?text|find)$", re.I)
# Non-search forms: no probe term is ever submitted to them (comment, login, contact, newsletter ...)
NOT_SEARCH_FORM_RE = re.compile(r"(login|giris|giriş|signin|sign-in|register|kayit|kayıt|uye|üye|comment|yorum|"
                                r"contact|iletisim|iletişim|newsletter|bulten|bülten|subscribe|abone|sepet|cart|"
                                r"checkout|odeme|ödeme|password|sifre|şifre)", re.I)
_NL = r"(?<![a-zçğıöşü])"  # letter boundary: do not count 'ara' inside 'para' or 'order' inside 'border'
_NR = r"(?![a-zçğıöşü])"
SEARCH_HINT_RE = re.compile(_NL + r"(search\w*|arama|ara|aranan|find|query|sorgu)" + _NR, re.I)
SKIP_SELECT_RE = re.compile(_NL + r"(sort|sort_?by|order|order_?by|orderby|sirala|sırala|siralama|sıralama|limit|"
                            r"per_?page|page_?size|lang|dil|currency|para_?birimi|view|gorunum|görünüm)" + _NR, re.I)


@dataclass
class SimplePage:
    url: str
    title: str
    dom: str
    regions: list[Region] = field(default_factory=list)
    controls: list[Control] = field(default_factory=list)
    count_hints: list[str] = field(default_factory=list)
    scroll_hints: list[str] = field(default_factory=list)
    raw_chars: int = 0
    search_fields: list[SearchField] = field(default_factory=list)
    context_note: str = ""  # e.g. "this page is the result page of the search 'a'"

    def as_prompt(self, max_regions: int = 8) -> str:
        out = [f"URL: {self.url}", f"TITLE: {self.title}",
               f"(Original HTML {self.raw_chars} chars, simplified {len(self.dom)} chars)"]
        if self.context_note:
            out.append("NOTE: " + self.context_note)
        if self.search_fields:
            out.append("\n## SEARCH FIELD CANDIDATES (number for search_field)")
            out.extend(f.line() for f in self.search_fields)
        if self.count_hints:
            out.append("COUNT HINTS ON THE PAGE: " + " | ".join(self.count_hints[:8]))
        for h in self.scroll_hints:
            out.append("SCROLL OBSERVATION: " + h)
        out.append("\n## REPEATED REGION CANDIDATES (selector => real match count in the raw page, hidden elements included)")
        for i, r in enumerate(self.regions[:max_regions], 1):
            out.append(f"{i}. {r.selector}  => {r.count} matches | short: {r.short_selector} => {r.short_count} "
                       f"| distinct links: {r.distinct_links}\n   text: {r.sample_text}\n   sample html: {r.sample_html}")
        if self.controls:
            out.append("\n## BUTTON / LINK CANDIDATES")
            for c in self.controls[:25]:
                line = f"- [{c.kind}] css: {c.selector} (=> {c.count} matches) | text='{c.text}'"
                if c.xpath:
                    line += f" | xpath: {c.xpath} (=> {c.xpath_count})"
                if c.href:
                    line += f" | href={c.href}"
                out.append(line)
        out.append("\n## SIMPLIFIED DOM (repeats reduced to 2 samples)\n" + self.dom)
        return "\n".join(out)


# ---------------------------------------------------------------- helpers

def _classes(el) -> list[str]:
    return [c for c in (el.get("class") or "").split() if IDENT.match(c)]


def _sig(el) -> str:
    return el.tag + "".join("." + c for c in sorted(_classes(el)))


def _simple_sel(el, allow_id: bool = True) -> str:
    _id = el.get("id")
    if allow_id and _id and IDENT.match(_id) and not re.search(r"\d{3,}", _id):
        return f"#{_id}"
    cls = _classes(el)[:3]
    return el.tag + "".join("." + c for c in cls)


def css_path(el, depth: int = 4) -> str:
    parts, cur = [], el
    while cur is not None and isinstance(cur.tag, str) and cur.tag not in ("html", "body") and len(parts) < depth:
        s = _simple_sel(cur, allow_id=cur is not el or True)
        parts.append(s)
        if s.startswith("#"):
            break
        cur = cur.getparent()
    return " > ".join(reversed(parts)) or el.tag


def _count(root, selector: str) -> int:
    try:
        return len(root.cssselect(selector))
    except Exception:
        return -1


def _text(el, n: int = 160) -> str:
    t = re.sub(r"\s+", " ", " ".join(el.itertext())).strip()
    return t[:n]


def _drop(el) -> None:
    try:
        el.drop_tree()
    except Exception:
        p = el.getparent()
        if p is not None:
            p.remove(el)


def _is_element(el) -> bool:
    return isinstance(el.tag, str)


_NOISE_ATTR = re.compile(r"^(data-kh|style|data-bs-.*|data-(toggle|trigger|placement)|aria-hidden|tabindex|role|srcset|sizes|loading|width|height)$")


def _compact_sample(el) -> None:
    """Strip noise that does not help writing selectors from the sample card HTML (so all fields fit in the
    limited space): decorative attributes, on* handlers, long data-* blobs, comments, image URLs, svg/script
    and empty elements without attributes. Long attribute values are shortened."""
    for c in el.xpath(".//comment()"):
        _drop(c)
    for e in list(el.iter()):
        if not _is_element(e):
            continue
        if e.tag in ("svg", "script", "style", "noscript", "path"):
            _drop(e)
            continue
        for a in list(e.attrib):
            v = e.attrib[a]
            if _NOISE_ATTR.match(a) or (a.startswith("on") and a != "open") or (a.startswith("data-") and a != "data-src"
                                                                and len(v) > 80):
                del e.attrib[a]
            elif a == "class":
                cls = _classes(e)[:6]
                if cls:
                    e.attrib[a] = " ".join(cls)
                else:
                    del e.attrib[a]
            elif len(v) > 80:
                e.attrib[a] = v[:80] + "..."
        if e.tag == "img" and "src" in e.attrib:
            e.attrib["src"] = "…"
    for e in reversed(list(el.iter())):
        if _is_element(e) and e is not el and e.tag in ("div", "span") and not len(e) \
                and not (e.text or "").strip() and not e.attrib:
            _drop(e)


# ---------------------------------------------------------------- cleanup

def clean_tree(html: str, drop_hidden: bool = True):
    doc = lxml.html.document_fromstring(html or "<html><body></body></html>")
    for c in doc.xpath("//comment()"):
        _drop(c)
    for el in list(doc.iter(*DROP_TAGS)):
        _drop(el)
    head = doc.find("head")
    if head is not None:
        for el in list(head):
            if el.tag != "title":
                _drop(el)
    if drop_hidden:
        for el in list(doc.iter()):
            if _is_element(el) and el.get(HIDDEN_ATTR) == "1":
                _drop(el)
    for el in doc.iter():
        if not _is_element(el):
            continue
        for a in list(el.attrib):
            v = el.attrib[a]
            if a in KEEP_ATTRS:
                if a == "class":
                    el.attrib[a] = " ".join(_classes(el)[:6]) if _classes(el) else ""
                    if not el.attrib[a]:
                        del el.attrib[a]
                elif a in ("src", "data-src") and v.startswith("data:"):
                    el.attrib[a] = "data:..."
                elif len(v) > 120:
                    el.attrib[a] = v[:120] + "…"
            elif a.startswith("data-") and a != HIDDEN_ATTR and len(v) <= 60:
                pass  # short data-* attributes can be useful for selectors
            else:
                del el.attrib[a]
        # limit the number of data-* attributes
        datas = [a for a in el.attrib if a.startswith("data-") and a not in KEEP_ATTRS]
        for a in datas[4:]:
            del el.attrib[a]
    # remove empty elements (inside out)
    for el in reversed(list(doc.iter())):
        if not _is_element(el) or el.tag in ("img", "input", "button", "a", "br", "hr", "body", "html", "select"):
            continue
        if len(el) == 0 and not (el.text or "").strip() and not el.attrib:
            _drop(el)
    return doc


# ---------------------------------------------------------------- region detection

def find_regions(doc, raw=None, limit: int = 12) -> list[Region]:
    raw = doc if raw is None else raw
    body = doc.find("body")
    if body is None:
        return []
    cands = []
    for parent in body.iter():
        if not _is_element(parent) or len(parent) < 3:
            continue
        groups = defaultdict(list)
        for ch in parent:
            if _is_element(ch):
                groups[_sig(ch)].append(ch)
        for sig, items in groups.items():
            if len(items) < 3:
                continue
            sizes = [sum(1 for _ in it.iter()) for it in items[:20]]
            avg = sum(sizes) / len(sizes)
            texts = [_text(it, 60) for it in items[:10]]
            if avg < 2 and not any(texts):
                continue
            links = set()
            for it in items:
                for a in it.iter("a"):
                    if a.get("href"):
                        links.add(a.get("href"))
                if it.tag == "a" and it.get("href"):
                    links.add(it.get("href"))
            score = len(items) * min(avg, 40) * (1.5 if links else 1.0)
            cands.append((score, parent, items, links))
    cands.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for score, parent, items, links in cands:
        item_simple = _simple_sel(items[0], allow_id=False)
        sel = css_path(parent, depth=3) + " > " + item_simple
        if sel in seen:
            continue
        seen.add(sel)
        # The sample card is taken from the raw DOM: the executor also reads hidden elements, and some sites
        # keep data (e.g. the author on kitapyurdu) in a list-view block that is hidden at the moment.
        src = items[0]
        for s in (sel, item_simple):
            try:
                hit = raw.cssselect(s)
            except Exception:
                hit = []
            if hit:
                src = hit[0]
                break
        sample = copy.deepcopy(src)
        sample.tail = None
        _compact_sample(sample)
        sample_html = lxml.html.tostring(sample, encoding="unicode")
        # the first regions are most likely the wanted records: let fields at the end of the card (author, brand) show too
        sample_html = re.sub(r"\s+", " ", sample_html)[:3500 if len(out) < 2 else 1500]
        out.append(Region(selector=sel, count=_count(raw, sel), short_selector=item_simple,
                          short_count=_count(raw, item_simple), distinct_links=len(links),
                          sample_html=sample_html, sample_text=_text(items[0], 200)))
        if len(out) >= limit:
            break
    return out


def _unique_sel(el, raw) -> tuple[str, int]:
    base = css_path(el, depth=4)
    n = _count(raw, base)
    if n == 1:
        return base, 1
    tries = []
    if "next" in (el.get("rel") or "").split():
        tries.append(base + '[rel~="next"]')
    for a in ("aria-label", "title", "data-testid"):
        v = el.get(a)
        if v and '"' not in v and len(v) < 60:
            tries.append(base + f'[{a}="{v}"]')
    for t in tries:
        if _count(raw, t) == 1:
            return t, 1
    return base, n


def find_controls(doc, raw=None) -> list[Control]:
    raw = doc if raw is None else raw
    body = doc.find("body")
    if body is None:
        return []
    out, seen = [], set()

    def add(kind, el):
        sel, n = _unique_sel(el, raw)
        key = (kind, sel)
        if key in seen:
            return
        seen.add(key)
        txt = _text(el, 60)
        label = el.get("aria-label", "")
        alt = ""
        if txt and "'" not in txt and len(txt) <= 30:
            alt = f"//{el.tag}[normalize-space(.)='{txt}']"
        elif not txt and label and "'" not in label:
            alt = f"//{el.tag}[@aria-label='{label}']"
        try:
            xc = len(raw.xpath(alt)) if alt else 0
        except Exception:
            xc = 0
        if xc == 0:
            alt = ""
        out.append(Control(kind=kind, selector=sel, text=txt or label, href=(el.get("href") or "")[:150],
                           count=n, xpath=alt, xpath_count=xc))

    for el in body.iter("a", "button", "input", "span", "div", "li"):
        if not _is_element(el):
            continue
        txt = _text(el, 80)
        label = " ".join(filter(None, [txt, el.get("aria-label", ""), el.get("title", ""), el.get("value", "")]))
        clickable = el.tag in ("a", "button", "input") or el.get("role") == "button"
        if "next" in (el.get("rel") or "").split() or (clickable and (NEXT_RE.match(txt) or re.search(r"\b(next|sonraki)\b",
                                                                                    el.get("aria-label", ""), re.I))):
            add("next", el)
        elif clickable and MORE_RE.search(label) and len(txt) < 60:
            add("more", el)
        elif clickable and COOKIE_RE.search(label) and len(txt) < 40:
            add("cookie", el)
    for el in body.iter():
        if _is_element(el) and re.search(r"(pagination|pager|paging|sayfalama)", el.get("class", "") + " " +
                                         el.get("id", ""), re.I):
            hrefs = [a.get("href") for a in el.iter("a") if a.get("href")][:6]
            psel = css_path(el, 3)
            out.append(Control(kind="pager", selector=psel, text=_text(el, 100),
                               href=" , ".join(hrefs)[:300], count=_count(raw, psel)))
            if sum(1 for c in out if c.kind == "pager") >= 3:
                break
    return out


# ---------------------------------------------------------------- view

def _collapse(doc) -> None:
    for parent in list(doc.iter()):
        if not _is_element(parent) or len(parent) < 4:
            continue
        groups = defaultdict(list)
        for ch in parent:
            if _is_element(ch):
                groups[_sig(ch)].append(ch)
        for sig, items in groups.items():
            if len(items) < 4:
                continue
            note = etree.Comment(f" +{len(items) - 2} more: {sig} ".replace("--", "-"))
            items[1].addnext(note)
            note.tail = items[1].tail
            items[1].tail = None
            for it in items[2:]:
                _drop(it)


def _truncate_text(doc, n: int) -> None:
    for el in doc.iter():
        if not _is_element(el):
            continue
        if el.text:
            el.text = re.sub(r"\s+", " ", el.text)
            if len(el.text) > n:
                el.text = el.text[:n] + "…"
        if el.tail:
            el.tail = re.sub(r"\s+", " ", el.tail)
            if len(el.tail) > n:
                el.tail = el.tail[:n] + "…"


def render_dom(doc, max_chars: int) -> str:
    view = copy.deepcopy(doc)
    _collapse(view)
    body = view.find("body")
    if body is None:
        body = view
    for n in (100, 50, 25):
        _truncate_text(view, n)
        s = lxml.html.tostring(body, encoding="unicode")
        s = re.sub(r"\s*\n\s*", "\n", re.sub(r"[ \t]+", " ", s))
        if len(s) <= max_chars:
            return s
    # still too long: drop attributes other than class/id/href
    for el in view.iter():
        if _is_element(el):
            for a in list(el.attrib):
                if a not in ("id", "class", "href"):
                    del el.attrib[a]
    s = lxml.html.tostring(body, encoding="unicode")
    s = re.sub(r"\s*\n\s*", "\n", re.sub(r"[ \t]+", " ", s))
    if len(s) > max_chars:
        half = max_chars // 2
        s = s[:half] + "\n<!-- ...[MIDDLE PART CUT]... -->\n" + s[-half:]
    return s


def _field_sel(el, raw) -> str:
    """A short css for the field that matches a single element on the page."""
    tries = []
    _id = el.get("id")
    if _id and IDENT.match(_id):
        tries.append(f"#{_id}")
    nm = el.get("name")
    if nm and '"' not in nm:
        tries.append(f'{el.tag}[name="{nm}"]')
        form = next(el.iterancestors("form"), None)
        if form is not None:
            tries.append(_simple_sel(form) + f' {el.tag}[name="{nm}"]')
    for a in ("placeholder", "aria-label"):
        v = el.get(a)
        if v and '"' not in v and len(v) < 60:
            tries.append(f'{el.tag}[{a}="{v}"]')
    for t in tries:
        if _count(raw, t) == 1:
            return t
    base, _n = _unique_sel(el, raw)
    return base


def find_search_fields(raw, limit: int = 8) -> list[SearchField]:
    """Search boxes and selects in search forms (in the raw DOM; hidden ones are marked)."""
    body = raw.find("body")
    if body is None:
        return []
    out: list[SearchField] = []
    seen = set()

    def hidden(el) -> bool:
        return any(a.get(HIDDEN_ATTR) == "1" for a in [el, *el.iterancestors()] if _is_element(a))

    def form_info(el):
        form = next(el.iterancestors("form"), None)
        if form is None:
            return "", "get", "", ""
        sub = ""
        for b in form.xpath(".//button[not(@type) or @type='submit'] | .//input[@type='submit' or @type='image']"):
            sub = _field_sel(b, raw)
            if _count(raw, sub) == 1:
                break
            sub = ""
        ctx = " ".join([form.get("action", ""), form.get("id", ""), form.get("class", ""), form.get("role", "")])
        return form.get("action", ""), (form.get("method") or "get").lower(), sub, ctx

    for el in body.iter("input", "select"):
        if not _is_element(el):
            continue
        typ = (el.get("type") or "text").lower()
        action, method, sub, ctx = form_info(el)
        if NOT_SEARCH_FORM_RE.search(ctx):
            continue
        hints = " ".join(filter(None, [el.get("name"), el.get("id"), el.get("placeholder"), el.get("aria-label"),
                                       el.get("title"), el.get("class")]))
        if el.tag == "input":
            if typ not in ("search", "text", ""):
                continue
            if not (typ == "search" or SEARCH_NAME_RE.match(el.get("name") or "") or SEARCH_HINT_RE.search(hints)
                    or SEARCH_HINT_RE.search(ctx)):
                continue
            kind, opts = "text", []
        else:
            if SKIP_SELECT_RE.search(hints):
                continue
            opts = []
            for o in el.iter("option"):
                lab = re.sub(r"\s+", " ", o.text_content() or "").strip()
                opts.append((o.get("value", lab), lab))
            if len(opts) < 3:
                continue
            kind = "select"
        sel = _field_sel(el, raw)
        if sel in seen:
            continue
        seen.add(sel)
        out.append(SearchField(index=len(out) + 1, kind=kind, selector=sel, name=el.get("name") or "",
                               label=(el.get("placeholder") or el.get("aria-label") or el.get("title") or "")[:60],
                               form_action=action[:150], form_method=method, submit=sub, options=opts,
                               hidden=hidden(el)))
        if len(out) >= limit:
            break
    # clear search forms first (GET, role/action/class 'search'), then visible ones, then text boxes before selects
    out.sort(key=lambda f: (f.form_method == "post", f.hidden, f.kind != "text"))
    for i, f in enumerate(out, 1):
        f.index = i
    return out


def simplify(html: str, url: str, max_chars: int = 40000) -> SimplePage:
    doc = clean_tree(html)
    # Counts are measured on the raw DOM the rule runs on (hidden elements included)
    raw = lxml.html.document_fromstring(html or "<html><body></body></html>")
    title_el = doc.find(".//title")
    title = (title_el.text or "").strip() if title_el is not None else ""
    body = doc.find("body")
    body_text = re.sub(r"\s+", " ", " ".join((body if body is not None else doc).itertext()))
    hints = []
    for m in COUNT_RE.finditer(body_text):
        s = body_text[max(0, m.start() - 30): m.end() + 10].strip()
        if s not in hints:
            hints.append(s)
    return SimplePage(url=url, title=title, dom=render_dom(doc, max_chars), regions=find_regions(doc, raw),
                      controls=find_controls(doc, raw), count_hints=hints[:10], raw_chars=len(html or ""),
                      search_fields=find_search_fields(raw))
