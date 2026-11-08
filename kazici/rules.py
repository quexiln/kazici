"""Rule schema: site-specific JSON produced by the LLM, then run without an LLM."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"

Kind = Literal["css", "xpath"]


class Sel(BaseModel):
    """A selector. kind=css or xpath. In item-relative selectors an xpath must start with '.'."""
    kind: Kind = "css"
    value: str

    @field_validator("value")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("empty selector")
        return v


class FieldRule(BaseModel):
    name: str = Field(description="Field name in the output, e.g. 'url', 'title', 'price'")
    selector: Optional[Sel] = Field(
        default=None, description="Selector relative to the item. If empty, the item itself is used.")
    attr: str = Field(
        default="text",
        description="'text' (visible text), 'html' or an attribute name ('href', 'src', 'data-id' ...)")
    multiple: bool = Field(default=False, description="If true, all matching values are taken as a list")
    transforms: list[Literal["strip", "absolute_url", "number", "lower", "collapse_ws", "strip_quotes"]] = Field(
        default_factory=lambda: ["strip"],
        description="Applied in order after regex. strip_quotes removes decorative wrapping quotes like “ ” « »")
    regex: Optional[str] = Field(default=None, description="Regex applied to the value; the first group (if any) is taken")
    required: bool = Field(default=False, description="Whether this field must be filled in every item")

    @field_validator("regex")
    @classmethod
    def _valid_regex(cls, v: Optional[str]) -> Optional[str]:
        if v:
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"invalid regex {v!r}: {e}")
        return v


def relativize_xpath(x: str) -> str:
    """An item-relative xpath must be relative: '//a' -> './/a', '(//a)[1]' -> '(.//a)[1]', and likewise for
    union branches and function arguments. Quoted strings are left untouched."""
    parts = re.split(r"""('[^']*'|"[^"]*")""", x)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r"(^|\||\(|,)(\s*)/", r"\1\2./", parts[i]) if i == 0 else \
            re.sub(r"(\||\(|,)(\s*)/", r"\1\2./", parts[i])
    return "".join(parts)


class Step(BaseModel):
    """A setup step performed on the page before extracting data."""
    action: Literal["click", "wait_for", "wait", "scroll_bottom", "press_escape"]
    selector: Optional[Sel] = None
    ms: int = Field(default=1000, description="Milliseconds for wait")
    optional: bool = Field(default=True, description="Do not fail if not found (e.g. a cookie banner)")


class Pagination(BaseModel):
    type: Literal["none", "next_button", "url_param", "infinite_scroll", "load_more"] = "none"
    selector: Optional[Sel] = Field(
        default=None, description="next_button / load_more: the element to click (required for these types)")
    param: Optional[str] = Field(
        default=None, description="url_param: query parameter name, e.g. 'page', 'p', 'pi', 'offset' (required)")
    page2_value: Optional[int] = Field(
        default=None, description="url_param: the param value used by the site's own link to PAGE 2 "
                                  "(e.g. 2 for ?page=2, 20 for ?offset=20). Page 1 is always the given URL.")
    step: int = Field(default=1, description="url_param: increment per page (1 for page numbers, 20 for offsets)")
    max_pages: int = Field(default=50, description="Max pages / clicks / scroll rounds")
    wait_ms: int = Field(default=1500, description="Fallback wait after each page change (ms)")

    @model_validator(mode="after")
    def _requirements(self):
        if self.type == "url_param" and not self.param:
            raise ValueError("pagination.type=url_param requires 'param'")
        if self.type in ("next_button", "load_more") and self.selector is None:
            raise ValueError(f"pagination.type={self.type} requires 'selector'")
        if self.step < 1:
            raise ValueError("pagination.step must be >= 1")
        # Pagination ends by itself, so let the rule also cover larger lists with the same layout
        floor = 50 if self.type != "none" else 1
        if self.max_pages != 0:  # 0 = unlimited (until the pages run out naturally)
            self.max_pages = max(floor, self.max_pages)
        return self

    def second_value(self) -> int:
        if self.page2_value is not None:
            return self.page2_value
        return 2 if self.step == 1 else self.step


class Expectations(BaseModel):
    """Validation criteria. The LLM sets them itself by looking at the page."""
    min_items_per_page: int = Field(default=1, description="Minimum number of items on the first page")
    min_total: int = Field(default=1, description="Minimum total number of unique items across all pages")
    unique_by: Optional[str] = Field(default=None, description="Field used for deduplication, e.g. 'url'")
    field_patterns: dict[str, str] = Field(
        default_factory=dict, description="Field name -> regex the value must match")
    min_fill_rate: float = Field(default=0.9, description="Minimum fill rate for required fields")


class Requested(BaseModel):
    """A single piece of data the user asked for, and the field that delivers it."""
    asked: str = Field(description="One thing the user asked for, in the USER'S OWN WORDS (e.g. 'price', 'author')")
    field: Optional[str] = Field(
        default=None, description="Name of the field in 'fields' that delivers exactly this. null ONLY if this "
                                  "data really does not exist on the page")


class RuleDraft(BaseModel):
    """The part the LLM fills in."""
    search_needed: bool = Field(
        default=False, description="true if the requested records must be reached through the site's SEARCH: "
                                   "the user wants records matching a name/title/term (e.g. 'videos titled X', "
                                   "'profile of Y'), or this page offers a search box instead of listing the "
                                   "records. Pick the field in search_field")
    search_field: Optional[int] = Field(
        default=None, description="search_needed: number of the field in SEARCH FIELD CANDIDATES to type into "
                                  "(or choose from, for a select)")
    search_terms: list[str] = Field(
        default_factory=list, description="search_needed: the exact term(s) to search, taken from the user's "
                                          "words (e.g. ['cat'] for 'videos titled cat'). EMPTY when the "
                                          "user wants ALL such records without naming one (the system then "
                                          "enumerates queries itself)")
    records_asked: str = Field(
        default="", description="Which records the user wants, in the user's own words (e.g. 'videos', 'books')")
    records_match: bool = Field(
        default=True, description="true if the item selector selects exactly that kind of record. false if the "
                                  "page does not contain those records (do NOT substitute other records)")
    in_linked_pages: bool = Field(
        default=False, description="true ONLY if the context makes it clear that the requested data is inside "
                                   "each record's own linked page (e.g. user wants videos and the page lists "
                                   "albums of videos; a product attribute shown only on the product page). Then "
                                   "item = those parent records with their link field, and the missing requested "
                                   "items stay field=null: a second step reads them from each record's page")
    requested: list[Requested] = Field(
        default_factory=list, description="EVERY separate piece of data the user named, one entry each, in order")
    goal_fields_note: str = Field(default="", description="Short note: which data you took from where")
    url_pattern: Optional[str] = Field(
        default=None, description="Python regex for the URLs of this site that share this page layout "
                                  "(e.g. all category pages). Must match the given URL(s).")
    fetch: Literal["browser", "http"] = "browser"
    setup: list[Step] = Field(default_factory=list)
    wait_for: Optional[Sel] = Field(default=None, description="Selector to wait for until the items have loaded")
    item: Sel = Field(description="Selector that selects each repeated record (product card etc.)")
    fields: list[FieldRule]
    pagination: Pagination = Field(default_factory=Pagination)
    expect: Expectations = Field(default_factory=Expectations)

    @model_validator(mode="after")
    def _relative_xpath(self):
        # Only list fields are item-relative; record page fields apply to the whole page (/html/... stays).
        for f in self.fields:
            if f.selector is not None and f.selector.kind == "xpath":
                f.selector.value = relativize_xpath(f.selector.value)
        return self


class DetailDraft(BaseModel):
    """The part the LLM writes for the record's own page (detail page)."""
    found: bool = Field(description="false if the requested data is not on this detail page either")
    requested: list[Requested] = Field(
        default_factory=list, description="Each MISSING requested item mapped to a field below (null if absent)")
    note: str = Field(default="", description="Short note: which data you took from where")
    wait_for: Optional[Sel] = Field(default=None, description="Selector that exists once the data is loaded")
    fields: list[FieldRule] = Field(
        default_factory=list, description="Selectors relative to the WHOLE detail page (css, or xpath starting "
                                          "with '//'). Use multiple=true for repeated values (e.g. every video "
                                          "link in an album). Names must differ from the list fields")


class Detail(DetailDraft):
    """Follows the list record's link one level down and takes extra fields. Never goes deeper."""
    link_field: str = Field(description="Field of the list record that holds the link to the detail page")
    fetch: Literal["browser", "http"] = "browser"
    # No separate limit: every record from the list gets its page opened. The page limit decides how many records.


def goal_issues(draft: RuleDraft) -> list[str]:
    """Only a mechanical inconsistency: a requested item is mapped to a field that is not in the rule.
    Requests not found on the page (field=null) are not errors, they are reported as warnings: otherwise the LLM
    is pushed to invent selectors for data that does not exist, or to wander off to other pages."""
    names = {f.name for f in draft.fields}
    return [f"Requested '{r.asked}' is mapped to field '{r.field}', but that field is not in 'fields'."
            for r in draft.requested if r.field and r.field not in names]


TR_ALPHABET = "abcçdefgğhıijklmnoöpqrsştuüvwxyz0123456789"


class Enumeration(BaseModel):
    """How to generate queries that find 'all' records when no specific term is given (chosen by testing during
    learning). empty/wildcard: an empty query or '*' lists everything. options: every option of the search's
    dropdown. prefix: letter prefixes; a prefix that hits the result cap is extended by one letter (a → aa, ab …)."""
    method: Literal["empty", "wildcard", "options", "prefix"]
    wildcard: str = "*"
    options: list[str] = Field(default_factory=list, description="options: option values (as seen during learning)")
    labels: list[str] = Field(default_factory=list, description="options: display labels of the options")
    alphabet: str = TR_ALPHABET
    min_chars: int = Field(default=1, ge=0, le=4)
    max_depth: int = Field(default=3, ge=1, le=5, description="prefix: maximum prefix length")
    result_cap: Optional[int] = Field(
        default=None, description="Maximum number of results the site shows for one query; a prefix reaching it is extended")


class Search(BaseModel):
    """Read from a search results page instead of a list page. The rule's item/fields/pagination part reads the
    results page. url: the results URL is built from a template ({query}); form: the term is typed into the box
    on the search page."""
    how: Literal["url", "form"] = "url"
    url_template: Optional[str] = Field(default=None, description="url: results URL with a {query} placeholder")
    page_url: Optional[str] = Field(default=None, description="Page that contains the search form (form and options)")
    input: Optional[Sel] = Field(default=None, description="Field where the search term is typed / selected")
    submit: Optional[Sel] = Field(default=None, description="form: button to click; Enter if none")
    is_select: bool = False
    option_values: list[str] = Field(default_factory=list, description="select: option values")
    option_labels: list[str] = Field(default_factory=list, description="select: display labels of the options")
    wait_ms: int = 2000
    queries: list[str] = Field(default_factory=list, description="Default terms taken from the request")
    terms: Literal["given", "all"] = Field(
        default="given", description="given: queries (or those given at run time); all: everything via enum")
    enum: Optional[Enumeration] = None
    max_queries: int = Field(default=200, ge=0, description="Default query limit (0 = unlimited)")

    @model_validator(mode="after")
    def _check(self):
        if self.how == "url" and (not self.url_template or "{query}" not in self.url_template):
            raise ValueError("search.how=url requires url_template containing {query}")
        if self.how == "form" and (self.input is None or not self.page_url):
            raise ValueError("search.how=form requires input and page_url")
        return self


_PH_WORD = re.compile(r"^(tümü|tüm|tum|hepsi|herhangi|all|any|seç|seçiniz|seçin|seciniz|select|choose|"
                      r"lütfen seçiniz|please select|please choose|select one)$", re.I)
_PH_END = re.compile(r"(seçiniz|seciniz|seçin|please select)\s*[:.]?$", re.I)
_PH_START = re.compile(r"^(tümü|tüm|tum|hepsi|all|any|seç|select|choose)(?![a-zçğıöşü])", re.I)
_PH_VALUES = {"0", "-1", "*", "all", "any", "tum", "tumu", "hepsi", "none"}


def is_placeholder_option(value: str, label: str) -> bool:
    """Not a real dropdown option: empty value, '-----', 'Seçiniz', 'Tümü', 'Kategori seçiniz'.
    Prefixes like 'Tüm Kategoriler' only count if the value is a placeholder too (0, -1, all): 'Any Time',
    'Seçkin Yayıncılık', 'Select Comfort' are real options."""
    v, l = (value or "").strip(), re.sub(r"\s+", " ", (label or "").strip())
    if not v or re.fullmatch(r"[-–—_.\s]*", l or "-"):
        return True
    if _PH_WORD.match(l) or _PH_END.search(l):
        return True
    return v.lower() in _PH_VALUES and (bool(_PH_START.match(l)) or not re.search(r"\d", l))


def fill_template(template: str, query: str) -> str:
    """Fills the {query} placeholder with encoding: quote_plus in the query string, quote in the path."""
    from urllib.parse import quote, quote_plus
    i = template.find("{query}")
    head = template[:i]
    # in the query string (after ?, before #) a space is '+'; in the path and # (JS router) it is %20
    in_query = "?" in head and "#" not in head
    return template.replace("{query}", quote_plus(query) if in_query else quote(query, safe=""))


def term_from_url(search: "Search", url: str) -> Optional[str]:
    """If a results page URL was given, the search term inside it (e.g. ...?query=python -> python)."""
    from urllib.parse import parse_qsl, unquote, unquote_plus, urlparse
    if search.how != "url" or not search.url_template:
        return None
    t = search.url_template
    tp, up = urlparse(t), urlparse(url)
    if (tp.netloc.lower().removeprefix("www."), tp.path) != (up.netloc.lower().removeprefix("www."), up.path)             and "{query}" not in tp.path:
        return None
    if "{query}" in tp.query:
        key = next((k for k, v in parse_qsl(tp.query, keep_blank_values=True) if v == "{query}"), None)
        hit = next((v for k, v in parse_qsl(up.query, keep_blank_values=True) if k == key), None)
        return hit
    if "{query}" in tp.fragment:
        a, _, b = tp.fragment.partition("{query}")
        f = up.fragment
        if f.startswith(a) and (not b or b in f[len(a):]):
            return unquote(f[len(a):f.index(b, len(a))] if b else f[len(a):])
        return None
    if "{query}" in tp.path and tp.netloc.lower().removeprefix("www.") == up.netloc.lower().removeprefix("www."):
        a, _, b = tp.path.partition("{query}")
        p = up.path
        if a.strip("/") == "":
            return None  # /{query} matches every URL: do not mistake category URLs for search terms
        if p.startswith(a) and p.endswith(b) and len(p) > len(a) + len(b) - 1:
            t = p[len(a):len(p) - len(b)] if b else p[len(a):]
            return None if "/" in t.strip("/") else unquote(t)
    return None


class Rule(RuleDraft):
    name: str
    site: str
    url_pattern: str = Field(description="Regex for the URLs this rule applies to")  # type: ignore[assignment]
    goal: str
    search: Optional[Search] = None
    detail: Optional[Detail] = None
    delay_ms: int = 1000
    version: int = 1
    meta: dict = Field(default_factory=dict)

    def matches(self, url: str) -> bool:
        if site_of(url) != self.site:
            return False
        if self.search is not None:
            # A search rule only matches its own search page or a results URL: category/home page
            # patterns must not grab the URLs of list rules.
            same = self.search.page_url and url.rstrip("/") == self.search.page_url.rstrip("/")
            return bool(same or term_from_url(self.search, url) is not None)
        return re.search(self.url_pattern, url) is not None

    def save(self, directory: Path = RULES_DIR) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.name}.json"
        # search_needed/search_field/search_terms are only for the LLM draft; the rule's search lives in `search`
        path.write_text(self.model_dump_json(indent=2, exclude_none=True,
                                             exclude={"search_needed", "search_field", "search_terms"}),
                        encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "Rule":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8-sig"))


def site_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def slug(text: str, maxlen: int = 40) -> str:
    tr = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.translate(tr)).strip("-").lower()
    return s[:maxlen].strip("-") or "rule"


def default_url_pattern(url: str, also: list[str] = ()) -> str:
    """Regex for the same path prefix (last path segment excluded, at most 3 segments). If extra sample
    pages on the same site (also) are given, the prefix is shortened to their common part so it matches all."""
    def keep_of(u: str) -> list[str]:
        parts = [x for x in urlparse(u).path.split("/") if x]
        return (parts[:-1] if len(parts) > 1 else parts)[:3]
    keep = keep_of(url)
    for u in also:
        if site_of(u) == site_of(url):
            other = keep_of(u)
            n = 0
            while n < min(len(keep), len(other)) and keep[n] == other[n]:
                n += 1
            keep = keep[:n]
    prefix = "/" + "/".join(keep) if keep else ""
    return r"^https?://(www\.)?" + re.escape(site_of(url)) + r"(:\d+)?" + re.escape(prefix) + r"(?:[/?#]|$)"


def choose_url_pattern(proposed: Optional[str], url: str, also: list[str]) -> str:
    """Use the LLM's proposed pattern if it matches all the sample URLs."""
    if proposed:
        try:
            rx = re.compile(proposed)
            p = urlparse(url)
            root = f"{p.scheme}://{p.netloc}/"
            probe = f"{p.scheme}://{p.netloc}/__kazici_probe__/x/y"
            too_broad = (url.rstrip("/") != root.rstrip("/") and rx.search(root)) or rx.search(probe)
            if all(rx.search(u) for u in [url, *also]) and not too_broad:
                return proposed
        except re.error:
            pass
    return default_url_pattern(url, also)


def all_rules(directory: Path = RULES_DIR, warn=None) -> list[tuple[Path, Rule]]:
    out = []
    for p in sorted(directory.glob("*.json")):
        try:
            out.append((p, Rule.load(p)))
        except Exception as e:
            if warn:
                warn(f"could not read rule {p.name}: {str(e).splitlines()[0] if str(e) else type(e).__name__}")
    return out


def find_rule(url: str, directory: Path = RULES_DIR) -> Optional[Rule]:
    cands = [r for _, r in all_rules(directory) if r.matches(url)]
    if not cands:
        return None
    # the longest (most specific) url_pattern wins; on a tie, the most recently learned
    return max(cands, key=lambda r: (len(r.url_pattern), r.meta.get("learned_at", "")))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
