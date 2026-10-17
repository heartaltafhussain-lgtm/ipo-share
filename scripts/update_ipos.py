"""
IPO Share updater (Playwright based).

- Chittorgarh ki mainboard/SME IPO list (JavaScript se load hoti hai) browser se padhta hai
- Har IPO ka detail page kholkar dates, price band, issue size aur subscription
  (QIB / NII / Retail / Employee / Total) nikalta hai
- Listing ke KEEP_DAYS (60 din = 2 mahine) baad IPO ko data se hata deta hai
- data/ipos.json update karta hai aur template.html se index.html rebuild karta hai
"""
import asyncio, json, re, datetime as dt, pathlib
from playwright.async_api import async_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "ipos.json"
HTML = ROOT / "index.html"
TEMPLATE = ROOT / "template.html"

KEEP_DAYS = 60  # listed IPO 2 mahine tak dikhega
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

ET_LISTED_URL = "https://economictimes.indiatimes.com/markets/ipo/recently-listed/mainboard"

SOURCES = [
    ("Mainboard", "https://www.chittorgarh.com/report/ipo-in-india-list-main-board-sme/82/mainboard/"),
]

DATE_FORMATS = ["%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y"]


def clean_date(s):
    s = s.replace(",", " ").replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\s+", "", s)  # "Fri, May 19" -> "May 19"
    return s


def parse_date(s):
    if not s:
        return None
    s = clean_date(s)
    for f in DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    return None


def parse_range(s):
    """'25 Apr to 3 May, 2006' -> (open, close). Single date bhi chalta hai."""
    if not s:
        return None, None
    year = re.search(r"\b(\d{4})\b", s)
    year = year.group(1) if year else None
    parts = re.split(r"\s+to\s+", s, maxsplit=1)
    if len(parts) == 2:
        start, end = parts[0].strip(), parts[1].strip()
        if year and not re.search(r"\b\d{4}\b", start):
            start = f"{start} {year}"
        return parse_date(start), parse_date(end)
    d = parse_date(s)
    return d, d


def to_num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def label_value(body, labels):
    """'Label<TAB>value' jaisi line se value nikalta hai."""
    for lab in labels:
        m = re.search(rf"{lab}[ \t]*\t[ \t]*([^\t\n]*)", body)
        if m:
            v = m.group(1).strip()
            if v and v not in ("[.]", "-", "NA"):
                return v
    return None


def parse_detail(body, rows):
    d = {}
    o, c = parse_range(label_value(body, ["IPO Date", "Issue Open", "Opening Date"]))
    o2, c2 = parse_range(label_value(body, ["Issue Close", "Closing Date", "IPO Close"]))
    d["open_date"] = o or o2
    d["close_date"] = c or c2 or o2
    d["listing_date"] = parse_date(label_value(body, ["Listed on", "Listing Date"]))
    d["price_band"] = label_value(body, ["Price Band"])
    ip = label_value(body, ["Issue Price"])
    m2 = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", ip or "")
    d["issue_price"] = to_num(m2.group(1)) if m2 else None

    m = re.search(r"agg\.?\s*up to\s*₹\s*([\d,]+(?:\.\d+)?)\s*Cr", body)
    d["issue_size_cr"] = to_num(m.group(1)) if m else None

    # Subscription table: pehla cell naam, dusra cell x-value (jaise 12.68)
    key_map = {"QIB (Ex Anchor)": "qib", "QIB (Ex. Anchor)": "qib", "QIB": "qib",
               "NII": "nii", "Retail": "retail", "Employee": "employee", "Total": "total"}
    sub = {}
    for r in rows:
        if len(r) < 2:
            continue
        name = r[0].strip()
        val = r[1].strip()
        if name in key_map and re.fullmatch(r"\d+\.\d+", val) and key_map[name] not in sub:
            sub[key_map[name]] = float(val)
    d["subscription"] = sub or None

    sym = label_value(body, ["Symbol"])
    if sym and "/" in sym:
        sym = sym.split("/")[-1]
    d["symbol"] = sym.strip() if sym else None
    d["sale_type"] = label_value(body, ["Sale Type"])
    return d


async def get_links(page, url):
    await page.goto(url, wait_until="networkidle", timeout=90000)
    await page.wait_for_timeout(2000)
    links = await page.eval_on_selector_all(
        "table a[href*='/ipo/']",
        "els => els.map(e => [e.getAttribute('href'), e.innerText.trim()])",
    )
    out, seen = [], set()
    for href, name in links:
        if not href or not re.search(r"/ipo/[^/]+/\d+/?$", href):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append({"url": href, "name": name})
    return out


async def get_detail(page, url):
    await page.goto(url, wait_until="networkidle", timeout=90000)
    await page.wait_for_timeout(1500)
    body = await page.inner_text("body")
    rows = await page.eval_on_selector_all(
        "table tr",
        "rs => rs.map(r => Array.from(r.querySelectorAll('th,td')).map(c => c.innerText.trim()))",
    )
    return parse_detail(body, rows)


def ipo_id(url):
    m = re.search(r"/ipo/([^/]+)/(\d+)/?$", url)
    return f"{m.group(1)}-{m.group(2)}" if m else re.sub(r"[^a-z0-9]+", "-", url.lower())


STOP = {"ltd", "limited", "ltd.", "india", "the", "of", "and", "&", "(india)", "(", ")"}


def name_key(name):
    """'Nityas Gems & Jewellery Ltd.' -> 'nityas gems' (pehle 2 shabd)."""
    name = re.sub(r"[^a-z0-9& ()]", " ", (name or "").lower())
    toks = [t for t in name.split() if t not in STOP]
    return " ".join(toks[:2])


def price_num(s):
    m = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", s or "")
    return to_num(m.group(1)) if m else None


async def fetch_listing_prices(page):
    """Economic Times 'recently listed mainboard' se issue price + listing (open) price."""
    out = {}
    try:
        await page.goto(ET_LISTED_URL, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(6000)
        rows = await page.eval_on_selector_all(
            "table tr",
            "rs => rs.map(r => Array.from(r.querySelectorAll('th,td')).map(c => c.innerText.trim()))",
        )
    except Exception as e:
        print("ET listing source failed", e)
        return out
    for r in rows:
        if len(r) < 5 or "Mainboard" not in r[0]:
            continue
        name = r[0].split("\n")[0]
        out[name_key(name)] = {"issue_price": price_num(r[3]), "listing_price": price_num(r[4])}
    print("ET listing prices:", len(out))
    return out


import urllib.request as _ur, time as _time, json as _json


def weekly_move(symbol, listing_date, ref_price=None):
    """Listing ke pehle 5 trading days mein sabse bada move (%). Yahoo Finance se."""
    if not symbol or not listing_date:
        return None
    ld = dt.date.fromisoformat(listing_date)
    p1 = int(dt.datetime.combine(ld - dt.timedelta(days=3), dt.time(0)).timestamp())
    p2 = int(dt.datetime.combine(ld + dt.timedelta(days=12), dt.time(0)).timestamp())
    data = None
    for host in ("query2", "query1"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}.NS?period1={p1}&period2={p2}&interval=1d"
        for attempt in range(3):
            try:
                req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with _ur.urlopen(req, timeout=20) as r:
                    data = _json.loads(r.read().decode())
                break
            except Exception:
                _time.sleep(3 + attempt * 3)
        if data:
            break
    if not data:
        return None
    try:
        res = data["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError):
        return None
    bars = []
    for t, o, c in zip(ts, q.get("open", []), q.get("close", [])):
        d = (dt.datetime.utcfromtimestamp(t) + dt.timedelta(hours=5, minutes=30)).date()
        if ld <= d <= ld + dt.timedelta(days=7) and c is not None:
            bars.append((d, o, c))
    bars = bars[:5]
    # Yahoo par naye IPO ka data kabhi adhoora hota hai: listing ke paas ka pehla bar hona zaroori hai
    if not bars or (bars[0][0] - ld).days > 2:
        return None
    ref = ref_price or bars[0][1]
    if not ref:
        return None
    moves = [(c / ref - 1) * 100 for _, _, c in bars]
    return round(max(moves, key=abs), 2)


def status(ipo, today):
    ld = ipo.get("listing_date")
    if ld and today >= dt.date.fromisoformat(ld):
        return "Listed"
    cd, od = ipo.get("close_date"), ipo.get("open_date")
    if cd and today <= dt.date.fromisoformat(cd):
        if od and today < dt.date.fromisoformat(od):
            return "Upcoming"
        return "Open"
    return "Closed"


def reference_date(ipo):
    """Retention ke liye: listing date, warna close date, warna pehli baar dekhi gayi date."""
    for key in ("listing_date", "close_date", "first_seen"):
        if ipo.get(key):
            return dt.date.fromisoformat(ipo[key])
    return None


async def main():
    today = dt.datetime.now(IST).date()
    db = json.loads(DATA.read_text(encoding="utf-8"))
    by_id = {i["id"]: i for i in db["ipos"]}

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()

        for seg, url in SOURCES:
            try:
                links = await get_links(page, url)
                print(seg, "list:", len(links), "IPO links")
            except Exception as e:
                print("list failed", url, e)
                continue

            for it in links:
                iid = ipo_id(it["url"])
                existing = by_id.get(iid)
                # Listed IPO ka data dobara fetch nahi karte
                if existing and existing.get("status") == "Listed" and existing.get("issue_price") is not None \
                        and existing.get("symbol") and existing.get("sale_type"):
                    continue
                try:
                    d = await get_detail(page, it["url"])
                except Exception as e:
                    print("detail failed", it["url"], e)
                    continue

                if not existing and not (d.get("listing_date") or d.get("close_date")):
                    continue  # date nahi mili, purana/anjaan IPO skip
                rec = existing or {
                    "id": iid, "company": it["name"], "symbol": "", "segment": seg,
                    "gmp": None, "applications": None, "first_seen": today.isoformat(),
                }
                rec.update({k: v for k, v in d.items() if k != "subscription" and v is not None})
                if d.get("symbol") and not rec.get("symbol"):
                    rec["symbol"] = d["symbol"]
                if d.get("subscription"):
                    rec["subscription"] = d["subscription"]
                rec["source"] = it["url"]
                rec["details_url"] = it["url"]
                rec["company"] = it["name"] or rec.get("company")
                if not existing:
                    ref = reference_date(rec)
                    if ref and (today - ref).days > KEEP_DAYS:
                        continue  # 60 din se purana IPO, add nahi karna
                    db["ipos"].append(rec)
                    by_id[iid] = rec
                print("  updated:", rec["company"], rec.get("subscription", {}).get("total") if rec.get("subscription") else "no subscription yet")

        # Listing price (open) + issue price: Economic Times se merge
        lp = await fetch_listing_prices(page)
        for i in db["ipos"]:
            hit = lp.get(name_key(i.get("company", "")))
            if not hit:
                continue
            if hit["listing_price"] is not None:
                i["listing_price"] = hit["listing_price"]
            if i.get("issue_price") is None and hit["issue_price"] is not None:
                i["issue_price"] = hit["issue_price"]

        await browser.close()

    # Listing ke 1 hafte ka move (Yahoo Finance)
    for i in db["ipos"]:
        if status(i, today) != "Listed" or not i.get("symbol") or not i.get("listing_date"):
            continue
        mv = weekly_move(i["symbol"], i["listing_date"], i.get("listing_price"))
        i["week1_move_pct"] = mv
        i["week1_flag"] = bool(mv is not None and abs(mv) >= 20)
        print("  week1:", i["company"], mv)
        _time.sleep(1)

    # Retention: 60 din baad hatao
    kept = []
    for i in db["ipos"]:
        if i.get("segment") != "Mainboard":
            continue
        ref = reference_date(i)
        if ref and (today - ref).days > KEEP_DAYS:
            continue
        i["status"] = status(i, today)
        kept.append(i)
    db["ipos"] = kept
    db["updated_at"] = dt.datetime.now(IST).isoformat(timespec="seconds")

    DATA.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
    html = TEMPLATE.read_text(encoding="utf-8").replace(
        "/*__DATA__*/null", json.dumps(db, ensure_ascii=False))
    HTML.write_text(html, encoding="utf-8")
    print("Done:", len(kept), "IPOs in dashboard")


if __name__ == "__main__":
    asyncio.run(main())
