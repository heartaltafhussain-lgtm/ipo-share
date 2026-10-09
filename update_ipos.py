"""
IPO Share updater.
- Reads data/ipos.json
- Pulls latest IPO list + subscription from configured sources (edit SOURCES)
- Removes listed IPOs that are more than KEEP_DAYS (30 = ~1 month) past listing
- Writes data/ipos.json and rebuilds index.html with embedded data
Run: python scripts/update_ipos.py
"""
import json, re, datetime as dt, pathlib, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "ipos.json"
HTML = ROOT / "index.html"
KEEP_DAYS = 30
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SOURCES = [
    # Chittorgarh subscription pages (mainboard + SME). Add more if needed.
    "https://www.chittorgarh.com/report/mainboard-ipo-list-in-india-bse-nse/83/",
    "https://www.chittorgarh.com/report/sme-ipo-subscription-status-live-bidding-bse-nse/22/",
]

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "ignore")

def num(s):
    try:
        return float(str(s).replace(",", "").replace("x", "").strip())
    except ValueError:
        return None

def parse_list(html):
    """Very loose parser: pulls company name + detail URL from list rows.
    Subscription is filled from the detail page (see enrich())."""
    items = []
    for href, name in re.findall(r'<a[^>]+href="(/ipo/[^"]+)"[^>]*>([^<]{3,80})</a>', html):
        if "/ipo/" in href and "subscription" not in name.lower():
            items.append({"name": name.strip(), "url": "https://www.chittorgarh.com" + href})
    seen, out = set(), []
    for it in items:
        if it["url"] not in seen:
            seen.add(it["url"]); out.append(it)
    return out

def enrich(item):
    """Extract QIB/NII/Retail/Employee/Total subscription from a detail page."""
    try:
        html = fetch(item["url"])
    except Exception as e:
        print("skip", item["url"], e); return None
    sub = {}
    for label, key in [("QIB", "qib"), ("NII", "nii"), ("Retail", "retail"), ("Employee", "employee"), ("Total", "total")]:
        m = re.search(rf"{label}[^<]*</t[dh]>\s*<t[dh][^>]*>\s*([\d.,]+)", html, re.I)
        if m:
            sub[key] = num(m.group(1))
    return {"subscription": sub, "source": item["url"], "details_url": item["url"]} if sub else None

def status(ipo, today):
    ld = ipo.get("listing_date")
    if ld:
        d = dt.date.fromisoformat(ld)
        if today >= d:
            return "Listed"
    cd = ipo.get("close_date")
    od = ipo.get("open_date")
    if cd and today <= dt.date.fromisoformat(cd):
        if od and today < dt.date.fromisoformat(od):
            return "Upcoming"
        return "Open"
    return "Closed"

def main():
    today = dt.datetime.now(IST).date()
    db = json.loads(DATA.read_text(encoding="utf-8"))
    by_id = {i["id"]: i for i in db["ipos"]}

    # 1. Add new IPOs found on sources
    for src in SOURCES:
        try:
            for it in parse_list(fetch(src)):
                iid = re.sub(r"[^a-z0-9]+", "-", it["name"].lower()).strip("-")
                if iid in by_id:
                    continue
                enr = enrich(it)
                if not enr:
                    continue
                new = {"id": iid, "company": it["name"], "symbol": "", "segment": "Mainboard" if "sme" not in src else "SME",
                       "open_date": None, "close_date": None, "listing_date": None, "price_band": None,
                       "issue_size_cr": None, "gmp": None, "subscription": enr["subscription"],
                       "applications": None, "source": enr["source"], "details_url": enr["details_url"]}
                db["ipos"].append(new); by_id[iid] = new
        except Exception as e:
            print("source failed", src, e)

    # 2. Drop listed IPOs older than KEEP_DAYS
    kept = []
    for i in db["ipos"]:
        ld = i.get("listing_date")
        if ld and (today - dt.date.fromisoformat(ld)).days > KEEP_DAYS:
            continue
        i["status"] = status(i, today)
        kept.append(i)
    db["ipos"] = kept
    db["updated_at"] = dt.datetime.now(IST).isoformat(timespec="seconds")

    DATA.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
    template = (ROOT / "template.html").read_text(encoding="utf-8")
    HTML.write_text(template.replace("/*__DATA__*/null", json.dumps(db, ensure_ascii=False)), encoding="utf-8")
    print("Done:", len(kept), "IPOs")

if __name__ == "__main__":
    main()
