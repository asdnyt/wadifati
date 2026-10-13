#!/usr/bin/env python3
"""Fetch public-service job announcements from emploi-public.ma into jobs.json.

Usage:
  python scraper/scrape.py --out site/jobs.json --pdf-dir site/arretes
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
ARRETE_URL = BASE + "/ar/" + quote("تحميل") + "/" + quote("المباريات") + "/arrete/"
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

SECTIONS = {
    "spec": ["التخصصات المطلوبة", "التخصص المطلوب", "التخصصات"],
    "cond": ["شروط الترشيح", "شروط الترشح", "شروط المشاركة"],
    "docs": ["الوثائق المطلوبة", "وثائق الملف", "ملف الترشيح"],
}
STOP = sum(SECTIONS.values(), []) + [
    "آخر أجل", "تاريخ إجراء", "مشاركة", "الإدارة المنظمة", "نبذة", "تفاصيل الإعلان",
    "اتصل", "إعلانات مشابهة", "المرفقات",
]
ORG_LABELS = ("الإدارة المنظمة", "المؤسسة", "الإدارة", "الجهة المنظمة", "الجهة")


def get(session, url):
    r = session.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r


def find_ids(html):
    return list(dict.fromkeys(ID_RE.findall(unquote(html))))


def collect_ids(session, max_pages, delay, debug):
    ids = []
    for p in range(1, max_pages + 1):
        url = LIST_URL if p == 1 else f"{LIST_URL}?page={p}"
        try:
            html = get(session, url).text
        except Exception as e:  # noqa: BLE001
            print(f"[list] page {p} failed: {e}")
            break
        if debug and p == 1:
            dump("list_page1.html", html)
        new = [i for i in find_ids(html) if i not in ids]
        print(f"[list] page {p}: {len(new)} new ids")
        if not new:
            break
        ids += new
        time.sleep(delay)
    if not ids:
        print("[list] nothing found on list pages, trying the home page")
        ids = find_ids(get(session, BASE + "/ar/").text)
    return ids


def dump(name, text):
    os.makedirs("debug", exist_ok=True)
    with open(os.path.join("debug", name), "w", encoding="utf-8") as f:
        f.write(text)


def to_date(m):
    d, mo, y, h = m.groups()
    return f"{int(y):04d}-{MONTHS[mo]:02d}-{int(d):02d}", (h or "").replace("h", ":").replace("H", ":")


def section(lines, keys):
    for i, line in enumerate(lines):
        if len(line) <= 60 and any(line.startswith(k) for k in keys):
            out = []
            for x in lines[i + 1:i + 16]:
                if any(x.startswith(s) for s in STOP) or len(x) > 110:
                    break
                x = x.lstrip("-•*·●▪ ").strip()
                if x:
                    out.append(x)
            if out:
                return out
    return []


def parse_detail(html, id_):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = soup.get_text("\n").translate(DIGITS)
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.split("\n")]
    lines = [l for l in lines if l]
    flat = " ".join(lines)

    title = ""
    for tag in ("h1", "h2"):
        el = soup.find(tag)
        if el and el.get_text(strip=True):
            title = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
            break
    if not title and soup.title:
        title = soup.title.get_text(strip=True).split(" - ")[0]

    org = ""
    for i, line in enumerate(lines):
        base = line.split(":")[0].strip()
        if base in ORG_LABELS:
            tail = line.split(":", 1)[1].strip() if ":" in line else ""
            org = tail or (lines[i + 1] if i + 1 < len(lines) else "")
            break

    m = re.search(r"آخر أجل[^0-9]{0,60}" + DATE_RE, flat)
    if not m:
        return None
    deadline, hour = to_date(m)
    m2 = re.search(r"تاريخ إجراء المباراة[^0-9]{0,30}" + DATE_RE, flat)
    exam = to_date(m2)[0] if m2 else ""

    n = 1
    m3 = re.search(r"عدد المناصب[^0-9]{0,15}(\d+)", flat) or re.search(r"(\d+)\s*(?:منصب|مناصب)", flat)
    if m3:
        n = int(m3.group(1))

    item = {"id": id_, "t": title, "o": org, "n": n, "d": deadline, "h": hour or "16:30"}
    if exam:
        item["e"] = exam
    for key, labels in SECTIONS.items():
        vals = section(lines, labels)
        if vals:
            item[key] = vals
    for line in lines:
        if len(line) > 80 and ("تعلن" in line or "يعلن" in line):
            item["desc"] = line[:400]
            break
    return item


def download_pdf(session, id_, pdf_dir):
    try:
        r = session.get(ARRETE_URL + id_, headers=HEADERS, timeout=60)
        ok = r.ok and (r.content[:4] == b"%PDF") and len(r.content) < 15_000_000
        if not ok:
            return None
        os.makedirs(pdf_dir, exist_ok=True)
        path = os.path.join(pdf_dir, f"{id_}.pdf")
        with open(path, "wb") as f:
            f.write(r.content)
        return f"{os.path.basename(pdf_dir.rstrip('/'))}/{id_}.pdf"
    except Exception as e:  # noqa: BLE001
        print(f"[pdf] {id_}: {e}")
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/jobs.json")
    ap.add_argument("--pdf-dir", default="")
    ap.add_argument("--max-pages", type=int, default=10)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--keep-expired", action="store_true")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()

    s = requests.Session()
    ids = collect_ids(s, a.max_pages, a.delay, a.debug)
    print(f"[main] {len(ids)} announcements to read")

    today = dt.date.today().isoformat()
    items, skipped = [], 0
    for k, id_ in enumerate(ids):
        try:
            html = get(s, DETAIL_URL + id_).text
            if a.debug and k == 0:
                dump("detail_first.html", html)
            it = parse_detail(html, id_)
        except Exception as e:  # noqa: BLE001
            print(f"[detail] {id_}: {e}")
            skipped += 1
            continue
        if not it or not it["t"]:
            skipped += 1
            continue
        if not a.keep_expired and it["d"] < today:
            continue
        if a.pdf_dir:
            pdf = download_pdf(s, id_, a.pdf_dir)
            if pdf:
                it["pdf"] = pdf
        items.append(it)
        time.sleep(a.delay)

    print(f"[main] kept {len(items)}, skipped {skipped}")
    if not items:
        print("ERROR: no announcements parsed - keeping the previous site untouched.")
        sys.exit(1)

    items.sort(key=lambda x: x["d"])
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"updated": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z", "items": items},
                  f, ensure_ascii=False, indent=1)
    print(f"[main] wrote {a.out}")


if __name__ == "__main__":
    main()
