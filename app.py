from __future__ import annotations

import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request

app = Flask(__name__)
BASE_URL = "https://lands.nuca.gov.eg"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140 Safari/537.36"
POSTBACK_RE = re.compile(r"__doPostBack\('([^']*)','([^']*)'\)")


def clean(s: str) -> str:
    return " ".join(s.replace("\xa0", " ").split())


def parse_rows(html: str) -> list[list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.find_all("tr"):
        cells = [clean(td.get_text(" ", strip=True)) for td in tr.find_all("td")]
        if len(cells) < 10:
            continue
        plot = cells[1]
        area = cells[2].replace(",", "")
        if plot and re.fullmatch(r"[0-9A-Za-z\-/]+", plot) and re.fullmatch(r"\d+(?:\.\d+)?", area):
            out.append(cells[:10])
    return out


def parse_pager(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        m = POSTBACK_RE.search(a.get("href", ""))
        if m:
            out.append({"text": clean(a.get_text(" ", strip=True)), "target": m.group(1), "argument": m.group(2)})
    return out


def hidden_fields(html: str):
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if not form:
        return {}, None
    data = {}
    for inp in form.find_all("input"):
        if inp.get("name") and (inp.get("type") or "").lower() == "hidden":
            data[inp["name"]] = inp.get("value", "")
    return data, form.get("action")


def summarize(rows):
    return {"count": len(rows), "plots": [r[1] for r in rows], "first": rows[0] if rows else None, "last": rows[-1] if rows else None}


def probe_zone(zone_id: int, page: int):
    url = f"{BASE_URL}/ar/ViewZone.aspx?ID={zone_id}"
    sess = requests.Session()
    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "ar-EG,ar;q=0.9,en;q=0.7"}
    r1 = sess.get(url, headers=headers, timeout=(8, 30))
    r1.raise_for_status()
    rows1 = parse_rows(r1.text)
    pager = parse_pager(r1.text)
    hidden, action = hidden_fields(r1.text)
    wanted = f"Page${page}"
    event = next((p for p in pager if p["argument"] == wanted), None) or next((p for p in pager if p["text"] == str(page)), None)
    result = {
        "zone_id": zone_id,
        "requested_page": page,
        "get_status": r1.status_code,
        "get_url": r1.url,
        "page1": summarize(rows1),
        "cookie_names": sorted(sess.cookies.keys()),
        "hidden_field_names": sorted(hidden.keys()),
        "viewstate_length": len(hidden.get("__VIEWSTATE", "")),
        "eventvalidation_length": len(hidden.get("__EVENTVALIDATION", "")),
        "pager": pager[:30],
        "selected_event": event,
    }
    if not event:
        result.update(ok=False, error="page postback event not found")
        return result
    data = dict(hidden)
    data["__EVENTTARGET"] = event["target"]
    data["__EVENTARGUMENT"] = event["argument"]
    data.setdefault("__LASTFOCUS", "")
    post_url = urljoin(r1.url, action or r1.url)
    post_headers = dict(headers)
    post_headers.update({"Referer": r1.url, "Origin": BASE_URL, "Content-Type": "application/x-www-form-urlencoded"})
    r2 = sess.post(post_url, headers=post_headers, data=data, timeout=(8, 30))
    r2.raise_for_status()
    rows2 = parse_rows(r2.text)
    result.update({
        "post_status": r2.status_code,
        "post_url": r2.url,
        "page2": summarize(rows2),
        "different_from_page1": [r[1] for r in rows1] != [r[1] for r in rows2],
    })
    result["ok"] = bool(rows2) and result["different_from_page1"]
    return result


@app.get("/")
def health():
    return jsonify({"service": "preis-nuca-pager", "status": "ok"})


@app.get("/probe")
def probe():
    try:
        zone_id = int(request.args.get("id", "4797"))
        page = int(request.args.get("page", "2"))
        if zone_id <= 0 or not 2 <= page <= 200:
            raise ValueError("invalid id/page")
        return jsonify(probe_zone(zone_id, page))
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502
