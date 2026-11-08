"""Applies the rule's item/fields part to HTML without an LLM (parsel: CSS + XPath)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from parsel import Selector

from .rules import FieldRule, Rule, Sel


@dataclass
class Extraction:
    items: list[dict] = field(default_factory=list)
    matched: int = 0  # number of elements the item selector matched
    errors: list[str] = field(default_factory=list)


def _select(node: Selector, sel: Sel, relative: bool):
    v = sel.value
    if sel.kind == "xpath":
        if relative and v.startswith("/"):
            v = "." + v
        return node.xpath(v)
    return node.css(v)


_HL_TAGS = ("em", "mark")  # search highlight; <b>/<strong>/<i> separate label and value ('<b>Stok</b>Var'), left alone
_HL_XPATH = ".//em|.//mark"


_GLUE_RIGHT = set(",.;:!?)]}%'’”…-")
_GLUE_LEFT = set("([{'‘“-")


def _glue(left: str, right: str) -> bool:
    if not left or not right or left[-1].isspace() or right[0].isspace():
        return False
    a, b = left[-1], right[0]
    return (a.isalnum() and b.isalnum()) or b in _GLUE_RIGHT or a in _GLUE_LEFT


def _text_with_highlights(root) -> str:
    """Joins texts with ' ' (old behaviour); only at the boundary between a highlight tag (<em>, <mark>) and the
    text flow it sits in, it adds no space: 'Why <em>Rust</em>, with' -> 'Why Rust, with',
    'Ru<em>st</em>' -> 'Rust'. Two adjacent elements ('<b>Ad</b><i>Yazar</i>') and '<b>Yazar:</b>Orhan' stay apart."""
    pieces: list[tuple[str, object, object]] = []  # (text, owner: the element for .text, the previous sibling for tail)

    def walk(e):
        if not isinstance(e.tag, str):
            return
        if e.text:
            pieces.append((e.text, e, None))
        for ch in e:
            walk(ch)
            if ch.tail:
                pieces.append((ch.tail, None, ch))
    root_tail, root.tail = root.tail, None
    try:
        walk(root)
    finally:
        root.tail = root_tail
    out = ""
    for i, (t, own, after) in enumerate(pieces):
        if i:
            pt, pown, pafter = pieces[i - 1]
            # boundary entering a highlight element (previous piece in the same flow) or leaving it (this piece is its tail)
            enter = own is not None and own.tag in _HL_TAGS and (
                (pown is not None and pown is own.getparent()) or (pafter is not None and pafter.getnext() is own))
            leave = after is not None and after.tag in _HL_TAGS and pown is after
            if (enter or leave) and (_glue(pt, t) or pt[-1:].isspace() or t[:1].isspace()):
                pass  # there is already a space at the boundary, or the word/punctuation is attached
            else:
                out += " "
        out += t
    return out


def _node_value(n: Selector, attr: str) -> str | None:
    root = n.root
    if isinstance(root, str):  # result of ::text, ::attr(), xpath text()/@attr
        return root
    if attr == "text":
        if root.xpath(_HL_XPATH):  # search highlight (<em>Rust</em>, with): the highlight tag must not split the word
            return _text_with_highlights(root)
        return " ".join(t for t in n.xpath(".//text()").getall())
    if attr == "html":
        return n.get()
    if hasattr(root, "get"):
        return root.get(attr)
    return None


# Thousands separator: dot, comma, NBSP and narrow NBSP. Not a plain space: "150 120" (old/new price) must not merge.
_NUM = re.compile(r"\d{1,3}(?:[.,  ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")


def parse_number(s: str):
    s = s or ""
    m = _NUM.search(s)
    if not m:
        return None
    t = re.sub(r"[\s  ]", "", m.group(0))
    i = m.start()
    # minus sign only at the start or after a space (not in cases like SKU-123)
    neg = i > 0 and s[i - 1] in "-−" and (i == 1 or not s[i - 2].isalnum())
    if "," in t and "." in t:
        # the last separator seen is the decimal one: 1.299,90 or 1,299.90
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
    elif "," in t:
        parts = t.split(",")
        t = t.replace(",", ".") if len(parts) == 2 and (len(parts[1]) != 3 or parts[0] == "0") \
            else t.replace(",", "")
    elif t.count(".") > 1 or (t.count(".") == 1 and len(t.split(".")[1]) == 3 and t.split(".")[0] != "0"):
        t = t.replace(".", "")
    try:
        f = -float(t) if neg else float(t)
        return int(f) if f.is_integer() else f
    except ValueError:
        return None


def _apply(v: str | None, f: FieldRule, base_url: str):
    if v is None:
        return None
    if f.regex:
        m = re.search(f.regex, v, re.S)
        if not m:
            return None
        v = m.group(1) if m.groups() else m.group(0)
    out = v
    for t in f.transforms:
        if out is None:
            break
        if t == "strip" or t == "collapse_ws":
            out = re.sub(r"\s+", " ", out).strip() if isinstance(out, str) else out
        elif t == "strip_quotes" and isinstance(out, str):
            out = out.strip().strip("\"'“”‘’«»„").strip()
        elif t == "lower" and isinstance(out, str):
            out = out.lower()
        elif t == "absolute_url" and isinstance(out, str):
            out = urljoin(base_url, out.strip()) if out.strip() else None
        elif t == "number":
            out = parse_number(str(out))
    if isinstance(out, str) and out == "":
        return None
    return out


def extract_html(html: str, base_url: str, rule: Rule) -> Extraction:
    ex = Extraction()
    sel = Selector(text=html or "<html></html>", base_url=base_url)
    base_href = sel.xpath("//base/@href").get()
    if base_href:
        base_url = urljoin(base_url, base_href.strip())
    try:
        items = _select(sel, rule.item, relative=False)
    except Exception as e:
        ex.errors.append(f"invalid item selector ({rule.item.kind}: {rule.item.value}): {e}")
        return ex
    ex.matched = len(items)
    bad_fields: set[str] = set()
    for it in items:
        row = _row(it, rule.fields, base_url, bad_fields, ex.errors)
        if any(v not in (None, []) for v in row.values()):
            ex.items.append(row)
    return ex


def _row(node: Selector, fields: list[FieldRule], base_url: str, bad_fields: set[str], errors: list[str]) -> dict:
    row = {}
    for f in fields:
        if f.name in bad_fields:
            row[f.name] = None
            continue
        try:
            nodes = _select(node, f.selector, relative=True) if f.selector else [node]
        except Exception as e:
            bad_fields.add(f.name)
            errors.append(f"invalid selector for field '{f.name}' ({f.selector.kind}: {f.selector.value}): {e}")
            row[f.name] = None
            continue
        try:
            vals = [_apply(_node_value(n, f.attr), f, base_url) for n in nodes]
        except re.error as e:
            bad_fields.add(f.name)
            errors.append(f"invalid regex for field '{f.name}' ({f.regex}): {e}")
            row[f.name] = None
            continue
        vals = [v for v in vals if v is not None]
        row[f.name] = vals if f.multiple else (vals[0] if vals else None)
    return row


def extract_page(html: str, base_url: str, fields: list[FieldRule]) -> tuple[dict, list[str]]:
    """Detail page: fields are applied to the whole page (a single record)."""
    sel = Selector(text=html or "<html></html>", base_url=base_url)
    base_href = sel.xpath("//base/@href").get()
    if base_href:
        base_url = urljoin(base_url, base_href.strip())
    errors: list[str] = []
    return _row(sel, fields, base_url, set(), errors), errors


def count_items(html: str, rule: Rule) -> int:
    try:
        return len(_select(Selector(text=html or "<html></html>"), rule.item, relative=False))
    except Exception:
        return 0
