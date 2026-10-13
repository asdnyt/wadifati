#!/usr/bin/env python3
"""Fetch OPEN public-service competition announcements from emploi-public.ma into jobs.json.

The list pages already contain title, organisation, posts and dates for each card,
so detail pages are fetched only for the (few) announcements that are still open.

Usage:
  python scraper/scrape.py --out site/jobs.json
  python scraper/scrape.py --debug      # also saves raw HTML pages to ./debug/
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from urllib.parse import quote, unquote

import requests
from bs4 import BeautifulSoup

BASE = "https://www.emploi-public.ma"
LIST_URL = BASE + "/ar/" + quote("قائمة-المباريات")
DETAIL_URL = BASE + "/ar/" + quote("تفاصيل") + "/" + quote("المباريات") + "/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; WazifatiBot/1.0)"}

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
ID_RE = re.compile(r"المباريات/(" + UUID + ")")
MONTHS = {
    "يناير": 1, "فبراير": 2, "مارس": 3, "أبريل": 4, "ابريل": 4, "ماي": 5, "مايو": 5,
    "يونيو": 6, "يوليوز": 7, "يوليو": 7, "غشت": 8, "أغسطس": 8, "شتنبر": 9,
    "سبتمبر": 9, "أكتوبر": 10, "اكتوبر": 10, "نونبر": 11, "نوفمبر": 11,
    "دجنبر": 12, "ديسمبر": 12,
}
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
DATE_RE = r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})(?:\s*[-–]\s*(\d{1,2}[:hH]\d{2}))?"
STAGES = ("الإعلان", "الاستدعاء للاختبار الكتابي", "الاستدعاء للمقابلات الشفوية", "النتائج")
SKIP_TITLES = ("لائحة", "النتائج", "نتائج", "الاستدعاء", "استدعاء")
TITLE_PREFIXES = ("مباراة لتوظيف ", "مباريات التوظيف :", "مباريات التوظيف:")
LABELS = (
    "تخصص", "الدرجة", "عدد المناصب", "نوع التوظيف", "نوع الإيداع", "موقع الإيداع",
    "معلومات إضافية", "رمز المباراة", "الإدارة المنظمة", "تاريخ النشر",
    "آخر أجل لإيداع الترشيحات", "آخر أجل لإيداع ملفات الترشيح", "تاريخ إجراء المباراة",
)


def get(session, url):
    r = session.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r


def dump(name, text):
    os.makedirs("debug", exist_ok=True)
    with open(os.path.join("debug", name), "w", encoding="utf-8") as f:
        f.write(text)


def clean(text):
    return re.sub(r"\s+", " ", text.translate(DIGITS)).strip()


def to_date(m):
    d, mo, y, h = m.groups()
    return f"{int(y):04d}-{MONTHS[mo]:02d}-{int(d):02d}", (h or "").replace("h", ":").replace("H", ":")


def strip_title(t):
    t = clean(t)
    for p in TITLE_PREFIXES:
        if t.startswith(p.strip()):
            t = t[len(p.strip()):].strip(" :")
    return t


def parse_cards(html):
    """Return {id: card dict} for every announcement card on a list page."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    cards = {}
    for a in soup.find_all("a", href=True):
        m = ID_RE.search(unquote(a["href"]))
        if not m:
            continue
        id_ = m.group(1)
        if id_ in cards:
            continue
        node = a
        found = None
        for _ in range(8):
            if node.parent is None:
                break
            node = node.parent
            ids = {ID_RE.search(unquote(x["href"])).group(1)
                   for x in node.find_all("a", href=True) if ID_RE.search(unquote(x["href"]))}
            if len(ids) > 1:
                break
            if "آخر أجل" in node.get_text():
                found = node
        if found is None:
            continue
        segs = [clean(s) for s in found.get_text("|").split("|")]
        segs = [s for s in segs if s]
        flat = " ".join(segs)
        m1 = re.search(r"آخر أجل[^0-9]{0,60}" + DATE_RE, flat)
        if not m1:
            continue
        deadline, hour = to_date(m1)
        m2 = re.search(r"تاريخ إجراء المباراة[^0-9]{0,30}" + DATE_RE, flat)
        h2 = found.find("h2")
        title = clean(h2.get_text(" ")) if h2 else segs[0]
        stage = next((s for s in segs if s in STAGES), "")
        org = ""
        for s in segs[segs.index(title) + 1:] if title in segs else segs[1:]:
            if s in STAGES or s.startswith("آخر") or s.startswith("تاريخ") or re.match(r"^\d", s):
                continue
            org = s
            break
        n = 1
        m3 = re.search(r"(\d+)\s*(?:منصب|مناصب)", flat)
        if m3:
            n = int(m3.group(1))
        cards[id_] = {
            "id": id_, "t": strip_title(title), "o": org, "n": n,
            "d": deadline, "h": hour or "16:30", "stage": stage,
            **({"e": to_date(m2)[0]} if m2 else {}),
        }
    return cards


def parse_detail(html):
    """Extra fields from a detail page: specialties, grade, deposit type, code, publish date."""
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    lines = [clean(l) for l in soup.get_text("\n").split("\n")]
    lines = [l for l in lines if l]

    def is_label(l):
        return l.rstrip(" :") in LABELS and (l.endswith(":") or l in LABELS)

    def value(label, many=False):
        for i, l in enumerate(lines):
            if l.rstrip(" :") == label and is_label(l):
                out = []
                for x in lines[i + 1:i + 18]:
                    if is_label(x) or x.startswith("اتصل"):
                        break
                    x = x.lstrip("-•*· ").strip()
                    if x:
                        out.append(x)
                    if not many:
                        break
                return out if many else (out[0] if out else "")
        return [] if many else ""

    extra = {}
    spec = value("تخصص", many=True)
    if spec:
        extra["spec"] = spec
    for key, label in (("g", "الدرجة"), ("dep", "نوع الإيداع"), ("code", "رمز المباراة")):
        v = value(label)
        if v:
            extra[key] = v
    pub = value("تاريخ النشر")
    mp = re.search(DATE_RE, pub) if pub else None
    if mp:
        extra["pub"] = to_date(mp)[0]
    return extra


def collect_open(session, today, max_pages, delay, debug):
    """Walk the list pages (announcements only) until no open announcement shows up for a while."""
    found, idle = {}, 0
    idle_limit = 3
    use_filter = True
    for p in range(1, max_pages + 1):
        qs = ("procedure=avis&" if use_filter else "") + f"page={p}"
        try:
            html = get(session, f"{LIST_URL}?{qs}").text
        except Exception as e:  # noqa: BLE001
            print(f"[list] page {p} failed: {e}")
            break
        if debug and p == 1:
            dump("list_page1.html", html)
        cards = parse_cards(html)
        if not cards and p == 1 and use_filter:
            print("[list] filter returned nothing, retrying without it")
            use_filter = False
            continue
        if not cards:
            break
        if p == 1 and any(c["stage"] not in ("", "الإعلان") or c["t"].startswith(SKIP_TITLES)
                          for c in cards.values()):
            print("[list] stage filter not applied by the site: scanning all pages")
            idle_limit = max_pages
        open_now = [c for c in cards.values()
                    if c["d"] >= today and c["stage"] in ("", "الإعلان")
                    and not c["t"].startswith(SKIP_TITLES)]
        for c in open_now:
            found.setdefault(c["id"], c)
        print(f"[list] page {p}: {len(cards)} cards, {len(open_now)} open")
        idle = 0 if open_now else idle + 1
        if idle >= idle_limit:
            break
        time.sleep(delay)
    return list(found.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/jobs.json")
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--max-details", type=int, default=400)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()

    s = requests.Session()
    today = dt.date.today().isoformat()
    items = collect_open(s, today, a.max_pages, a.delay, a.debug)
    print(f"[main] {len(items)} open announcements")

    for k, it in enumerate(items[:a.max_details]):
        try:
            html = get(s, DETAIL_URL + it["id"]).text
            if a.debug and k == 0:
                dump("detail_first.html", html)
            it.update(parse_detail(html))
        except Exception as e:  # noqa: BLE001
            print(f"[detail] {it['id']}: {e}")
        time.sleep(a.delay)
    for it in items:
        it.pop("stage", None)

    if not items:
        print("ERROR: no open announcements parsed - keeping the previous site untouched.")
        sys.exit(1)

    items.sort(key=lambda x: x["d"])
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"updated": now, "items": items}, f, ensure_ascii=False, indent=1)
    print(f"[main] wrote {a.out} with {len(items)} items")


if __name__ == "__main__":
    main()
