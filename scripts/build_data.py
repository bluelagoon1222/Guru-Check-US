#!/usr/bin/env python3
"""
Guru-Check-US data builder (runs daily on GitHub Actions).

Sources
  SEC EDGAR   13F (quarterly holdings), Schedule 13D/13G, Form 4      -> structured, "확정"
  House Clerk STOCK Act periodic transaction reports (PDF with text)  -> "확정" (amount ranges)
  White House / OGE  President & officials 278-T (scanned PDF -> OCR) -> "OCR" (needs review)
  OpenFIGI    CUSIP -> ticker mapping (cached)

Output: data/guru.json  (+ caches data/*.json so PDFs/OCR are processed once)
Each section is isolated: a failure in one source never blocks the others.
"""
import datetime as dt
import io
import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CFG = json.loads((ROOT / "config" / "people.json").read_text(encoding="utf-8"))
SEC_UA = os.environ.get("SEC_USER_AGENT", "").strip()
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
TODAY = dt.date.today()
DIAG = []                       # problems worth showing on the page / reading after a run


def log(*a):
    print(*a, flush=True)


def diag(src, msg):
    DIAG.append({"src": src, "msg": str(msg)[:300]})
    log("DIAG", src, msg)


def load_json(name, default):
    p = DATA / name
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(name, obj):
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / name).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def d(s):
    return dt.date.fromisoformat(s)


# ================================================================== HTTP
_last = [0.0]


def sec_get(url, as_json=True):
    for attempt in range(4):
        wait = 0.13 - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            r = requests.get(url, headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip"}, timeout=60)
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                return r.json() if as_json else r.text
        except requests.RequestException:
            pass
        time.sleep(2 * (attempt + 1))
    raise RuntimeError("SEC request failed: " + url)


def web_get(url, binary=False, timeout=60):
    for attempt in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=timeout)
            if r.status_code == 200:
                return r.content if binary else r.text
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        time.sleep(2 * (attempt + 1))
    return None


def tag(blk, name):
    m = re.search(rf"<(?:\w+:)?{name}>\s*(.*?)\s*</(?:\w+:)?{name}>", blk, re.S)
    return m.group(1).strip() if m else ""


def edgar_url(cik, accn):
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/{accn}-index.htm"


# ================================================================== SEC ticker list (CIK -> ticker)
TICKER_BY_CIK = {}


def load_sec_tickers():
    js = sec_get("https://www.sec.gov/files/company_tickers.json") or {}
    for row in js.values():
        TICKER_BY_CIK.setdefault(int(row["cik_str"]), row["ticker"].upper())


# ================================================================== 13F
def parse_13f(xml):
    out = {}
    for blk in re.findall(r"<(?:\w+:)?infoTable>(.*?)</(?:\w+:)?infoTable>", xml, re.S):
        cusip = tag(blk, "cusip").upper()
        pc = tag(blk, "putCall").upper()
        key = cusip + ("|" + pc if pc else "")
        try:
            val = int(float(tag(blk, "value") or 0))
            sh = int(float(tag(blk, "sshPrnamt") or 0))
        except ValueError:
            continue
        o = out.setdefault(key, {"cusip": cusip, "name": tag(blk, "nameOfIssuer"), "cls": tag(blk, "titleOfClass"),
                                 "put_call": pc, "value": 0, "shares": 0, "unit": tag(blk, "sshPrnamtType")})
        o["value"] += val
        o["shares"] += sh
    return out


def latest_13f_filings(sub):
    rc = sub["filings"]["recent"]
    rows = []
    for i, f in enumerate(rc["form"]):
        if f in ("13F-HR", "13F-HR/A"):
            rows.append({"accn": rc["accessionNumber"][i], "filed": rc["filingDate"][i], "form": f,
                         "period": rc.get("reportDate", [""] * len(rc["form"]))[i]})
    by_period = {}
    for r in sorted(rows, key=lambda x: (x["form"] != "13F-HR", x["filed"])):
        if r["period"]:
            by_period.setdefault(r["period"], r)     # original first; an amendment only if no original exists
    return [by_period[k] for k in sorted(by_period, reverse=True)]


def fetch_13f_table(cik, accn):
    nd = accn.replace("-", "")
    idx = sec_get(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nd}/index.json")
    if not idx:
        return None
    names = [it["name"] for it in idx["directory"]["item"]]
    xmls = [n for n in names if n.lower().endswith(".xml") and "primary_doc" not in n.lower()]
    best = None
    for n in xmls:
        txt = sec_get(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nd}/{n}", as_json=False) or ""
        if "infoTable" in txt:
            best = txt
            break
    if not best:
        return None
    rows = parse_13f(best)
    # Some filers still report <value> in thousands of dollars: detect via implied price per share
    px = sorted(o["value"] / o["shares"] for o in rows.values() if o["unit"] == "SH" and o["shares"] > 0 and not o["put_call"])
    if px and px[len(px) // 2] < 0.5:
        for o in rows.values():
            o["value"] *= 1000
        rows["__scaled__"] = True
    return rows


def diff_13f(now, prev):
    ch = []
    for k, o in now.items():
        p = prev.get(k)
        if not p:
            ch.append(dict(o, kind="new", prev_shares=0, prev_value=0, pct=None))
        elif o["shares"] != p["shares"]:
            pct = (o["shares"] - p["shares"]) / p["shares"] if p["shares"] else None
            ch.append(dict(o, kind="add" if o["shares"] > p["shares"] else "cut",
                           prev_shares=p["shares"], prev_value=p["value"], pct=pct))
    for k, p in prev.items():
        if k not in now:
            ch.append(dict(p, kind="exit", prev_shares=p["shares"], prev_value=p["value"],
                           shares=0, value=0, pct=-1.0))
    return ch


# ================================================================== Schedule 13D / 13G
BEN_FORMS = {"SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A", "SC 13G", "SC 13G/A", "SCHEDULE 13G", "SCHEDULE 13G/A"}


def subject_of(cik, accn):
    html = sec_get(edgar_url(cik, accn), as_json=False) or ""
    for m in re.finditer(r'class="companyName">\s*([^<(]+?)\s*\((Subject|Filed by|Reporting)\)[^<]*<acronym[^>]*>CIK</acronym>:\s*<a[^>]*>(\d+)', html):
        if m.group(2) == "Subject":
            return m.group(1).strip(), int(m.group(3))
    return None, None


# ================================================================== Form 4
TX_CODE = {"P": "장내 매수", "S": "장내 매도", "A": "보상 지급", "M": "옵션 행사", "F": "세금 납부용 처분",
           "G": "증여", "C": "전환", "D": "회사 반환", "J": "기타", "X": "옵션 행사", "W": "상속"}


def parse_form4(xml, owner_cik):
    owners = [int(x) for x in re.findall(r"<rptOwnerCik>\s*0*(\d+)\s*</rptOwnerCik>", xml)]
    if owner_cik not in owners:
        return None
    txs = []
    for blk in re.findall(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", xml, re.S):
        def v(name):
            m = re.search(rf"<{name}>.*?<value>\s*(.*?)\s*</value>", blk, re.S)
            return m.group(1).strip() if m else ""
        code = tag(blk, "transactionCode")
        try:
            sh = float(v("transactionShares") or 0)
            px = float(v("transactionPricePerShare") or 0)
            after = float(v("sharesOwnedFollowingTransaction") or 0)
        except ValueError:
            sh, px, after = 0, 0, 0
        txs.append({"date": v("transactionDate"), "code": code, "ad": v("transactionAcquiredDisposedCode"),
                    "shares": sh, "price": px, "after": after, "security": v("securityTitle")})
    return {"issuer": tag(xml, "issuerName"), "ticker": tag(xml, "issuerTradingSymbol").upper(), "tx": txs}


# ================================================================== SEC people
def build_sec(feed):
    if "@" not in SEC_UA:
        diag("sec", "SEC_USER_AGENT secret missing")
        return []
    load_sec_tickers()
    cut_120 = (TODAY - dt.timedelta(days=120)).isoformat()
    figi_needed = set()
    out = []
    for grp, plist in (("investor", CFG["investors_13f"]), ("ceo", CFG["insiders_form4"])):
        for p in plist:
            person = {"id": p["id"], "name_ko": p["name_ko"], "group": grp, "entity": p.get("entity", ""),
                      "cik": p["cik"], "archived": bool(p.get("archived")), "tx": [], "filings": []}
            try:
                sub = sec_get(f"https://data.sec.gov/submissions/CIK{p['cik']:010d}.json")
                ename = (sub or {}).get("name", "")
                if p["expect"].upper() not in ename.upper():
                    diag("sec", f"{p['id']}: CIK name mismatch ({ename})")
                    continue
                person["entity_sec"] = ename
                rc = sub["filings"]["recent"]
                # ---- 13F
                if grp == "investor":
                    fl = latest_13f_filings(sub)
                    if fl:
                        now = fetch_13f_table(p["cik"], fl[0]["accn"]) or {}
                        prev = fetch_13f_table(p["cik"], fl[1]["accn"]) if len(fl) > 1 else {}
                        prev = prev or {}
                        scaled = bool(now.pop("__scaled__", False))
                        prev.pop("__scaled__", None)
                        tot = sum(o["value"] for o in now.values()) or 1
                        hold = sorted(now.values(), key=lambda o: -o["value"])
                        changes = diff_13f(now, prev)
                        changes.sort(key=lambda c: -max(c["value"], c["prev_value"]))
                        person["f13"] = {
                            "period": fl[0]["period"], "filed": fl[0]["filed"], "accn": fl[0]["accn"],
                            "prev_period": fl[1]["period"] if len(fl) > 1 else None,
                            "total_value": tot, "positions": len(now),
                            "url": edgar_url(p["cik"], fl[0]["accn"]),
                            "holdings": [dict(h, weight=h["value"] / tot) for h in hold[:60]],
                            "changes": {k: [c for c in changes if c["kind"] == k][:40] for k in ("new", "add", "cut", "exit")},
                            "counts": {k: sum(1 for c in changes if c["kind"] == k) for k in ("new", "add", "cut", "exit")},
                            "scaled_thousands": scaled,
                        }
                        for h in hold[:60]:
                            figi_needed.add(h["cusip"])
                        for lst in person["f13"]["changes"].values():
                            for c in lst:
                                figi_needed.add(c["cusip"])
                        if fl[0]["filed"] >= cut_120:
                            feed.append({"date": fl[0]["filed"], "pid": p["id"], "kind": "13F",
                                         "period": fl[0]["period"], "counts": person["f13"]["counts"],
                                         "top": {k: [c["cusip"] for c in person["f13"]["changes"][k][:3]] for k in ("new", "add", "cut", "exit")},
                                         "url": person["f13"]["url"], "label": "확정"})
                # ---- 13D / 13G (filer side)
                n_ben = 0
                for i, f in enumerate(rc["form"]):
                    if f in BEN_FORMS and rc["filingDate"][i] >= cut_120 and n_ben < 12:
                        n_ben += 1
                        accn = rc["accessionNumber"][i]
                        name, scik = subject_of(p["cik"], accn)
                        if name is None or scik == p["cik"]:
                            continue
                        item = {"date": rc["filingDate"][i], "pid": p["id"], "kind": "13D" if "13D" in f else "13G",
                                "form": f, "issuer": name, "ticker": TICKER_BY_CIK.get(scik, ""),
                                "amend": f.endswith("/A"), "url": edgar_url(p["cik"], accn), "label": "확정"}
                        person["filings"].append(item)
                        feed.append(item)
                # ---- Form 4 (as reporting owner)
                n4 = 0
                for i, f in enumerate(rc["form"]):
                    if f == "4" and rc["filingDate"][i] >= cut_120 and n4 < 25:
                        accn = rc["accessionNumber"][i]
                        doc = rc["primaryDocument"][i].split("/")[-1]
                        xml = sec_get(f"https://www.sec.gov/Archives/edgar/data/{p['cik']}/{accn.replace('-', '')}/{doc}", as_json=False) or ""
                        f4 = parse_form4(xml, p["cik"])
                        if not f4:
                            continue
                        n4 += 1
                        for t in f4["tx"]:
                            row = {"date": t["date"] or rc["filingDate"][i], "filed": rc["filingDate"][i], "pid": p["id"],
                                   "kind": "Form4", "issuer": f4["issuer"], "ticker": f4["ticker"],
                                   "code": t["code"], "action": TX_CODE.get(t["code"], t["code"]), "ad": t["ad"],
                                   "shares": t["shares"], "price": t["price"], "value": t["shares"] * t["price"],
                                   "after": t["after"], "url": edgar_url(p["cik"], accn), "label": "확정"}
                            person["tx"].append(row)
                            if t["code"] in ("P", "S"):
                                feed.append(row)
                person["last_activity"] = max([x.get("filed", x["date"]) for x in person["tx"]] +
                                              [x["date"] for x in person["filings"]] +
                                              ([person["f13"]["filed"]] if person.get("f13") else []) or [""])
            except Exception as e:
                diag("sec", f"{p['id']}: {e}")
            out.append(person)
            log("sec", p["id"], "ok")
    map_cusips(figi_needed)
    return out


# ================================================================== OpenFIGI CUSIP -> ticker
def map_cusips(cusips):
    cache = load_json("cusip_map.json", {})
    todo = [c for c in sorted(cusips) if c and c not in cache]
    log("figi todo", len(todo))
    for i in range(0, len(todo), 10):
        chunk = todo[i:i + 10]
        jobs = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in chunk]
        for attempt in range(3):
            try:
                r = requests.post("https://api.openfigi.com/v3/mapping", json=jobs,
                                  headers={"Content-Type": "application/json"}, timeout=30)
                if r.status_code == 429:
                    time.sleep(30)
                    continue
                res = r.json()
                for c, x in zip(chunk, res):
                    data = (x or {}).get("data") or []
                    cache[c] = data[0].get("ticker", "") if data else ""
                break
            except Exception:
                time.sleep(5)
        time.sleep(2.6)                  # anonymous limit: 25 requests / minute
    save_json("cusip_map.json", cache)
    CUSIP_TICKER.update(cache)


CUSIP_TICKER = {}


# ================================================================== House PTRs
PTR_RX = re.compile(
    r"(?:(SP|JT|DC)\s+)?(?P<asset>[^\[\]]{2,200}?)\s*\[(?P<atype>[A-Z]{2})\]\s+"
    r"(?P<tx>P|S \(partial\)|S|E)\s+(?P<d1>\d{2}/\d{2}/\d{4})\s+(?P<d2>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amt>\$[\d,]+\s*-\s*\$[\d,]+|Over \$[\d,]+|\$[\d,]+\s*-)")


def parse_ptr_text(text):
    t = text.replace("\x00", "")
    t = re.sub(r"Filing ID #\d+|Clerk of the House.*?20515", " ", t)
    flat = re.sub(r"\s+", " ", t)
    rows = []
    for m in PTR_RX.finditer(flat):
        raw = (m.group(1) + " " if m.group(1) else "") + m.group("asset")
        raw = re.sub(r".*\$200\?\s*", "", raw)
        owner = ""
        own = list(re.finditer(r"(?:^|\s)(SP|JT|DC)\s", raw))
        if own:
            owner = own[-1].group(1)
            raw = raw[own[-1].end():]
        else:
            cut = list(re.finditer(r"S: (?:New|Amended)\s", raw))
            if cut:
                raw = raw[cut[-1].end():]
        asset = raw.strip(" -")
        tk = re.search(r"\(([A-Z][A-Z.]{0,6})\)", asset)
        amt = m.group("amt").replace(" ", "")
        rows.append({"owner": owner, "asset": asset[-140:], "ticker": tk.group(1) if tk else "",
                     "atype": m.group("atype"), "tx": {"P": "매수", "S": "매도", "S (partial)": "일부 매도", "E": "교환"}[m.group("tx")],
                     "date": dt.datetime.strptime(m.group("d1"), "%m/%d/%Y").date().isoformat(),
                     "notified": dt.datetime.strptime(m.group("d2"), "%m/%d/%Y").date().isoformat(),
                     "amount": amt})
    return rows


def build_house(feed):
    try:
        import pdfplumber
    except ImportError:
        diag("house", "pdfplumber missing")
        return []
    cache = load_json("house_cache.json", {})
    window = CFG["congress"].get("window_days", 180)
    since = TODAY - dt.timedelta(days=window)
    members = []
    for yr in sorted({TODAY.year, since.year}):
        z = web_get(f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{yr}FD.zip", binary=True)
        if not z:
            diag("house", f"index {yr} not downloaded")
            continue
        zf = zipfile.ZipFile(io.BytesIO(z))
        xml = zf.read(next(n for n in zf.namelist() if n.lower().endswith(".xml"))).decode("utf-8", "replace")
        for blk in re.findall(r"<Member>(.*?)</Member>", xml, re.S):
            if tag(blk, "FilingType") != "P":
                continue
            fd = dt.datetime.strptime(tag(blk, "FilingDate"), "%m/%d/%Y").date()
            if fd < since:
                continue
            members.append({"doc": tag(blk, "DocID"), "year": yr, "filed": fd.isoformat(),
                            "first": tag(blk, "First"), "last": tag(blk, "Last"), "state": tag(blk, "StateDst")})
    log("house ptr filings", len(members))
    for m in members:
        if m["doc"] in cache:
            continue
        pdf = web_get(f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{m['year']}/{m['doc']}.pdf", binary=True)
        rows, ok = [], False
        if pdf:
            try:
                with pdfplumber.open(io.BytesIO(pdf)) as doc:
                    text = "\n".join((pg.extract_text() or "") for pg in doc.pages)
                ok = len(text.strip()) > 80
                rows = parse_ptr_text(text) if ok else []
            except Exception as e:
                diag("house", f"{m['doc']}: {e}")
        cache[m["doc"]] = {"rows": rows, "text": ok}
        time.sleep(0.25)
    save_json("house_cache.json", cache)
    # group by member
    by = {}
    for m in members:
        key = f"{m['first']} {m['last']}|{m['state']}"
        b = by.setdefault(key, {"first": m["first"], "last": m["last"], "state": m["state"], "filings": [], "tx": []})
        c = cache.get(m["doc"], {})
        url = f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{m['year']}/{m['doc']}.pdf"
        b["filings"].append({"doc": m["doc"], "filed": m["filed"], "url": url, "parsed": len(c.get("rows", [])), "text": c.get("text", False)})
        for r in c.get("rows", []):
            b["tx"].append(dict(r, filed=m["filed"], url=url))
    always = {(a["last"].lower(), a.get("first", "").lower()) for a in CFG["congress"]["always"]}
    ranked = sorted(by.values(), key=lambda b: -len(b["tx"]))
    chosen = [b for b in by.values() if (b["last"].lower(), b["first"].lower()) in always]
    for b in ranked:
        if len(chosen) >= CFG["congress"]["auto_top_n"] + len(always):
            break
        if b not in chosen and b["tx"]:
            chosen.append(b)
    out = []
    for b in chosen:
        pid = "h_" + re.sub(r"[^a-z]", "", (b["last"] + b["first"]).lower())[:20]
        ko = next((a["name_ko"] for a in CFG["congress"]["always"] if a["last"].lower() == b["last"].lower()), "")
        person = {"id": pid, "name_ko": ko or f"{b['first']} {b['last']}", "name_en": f"{b['first']} {b['last']}",
                  "group": "congress", "entity": f"하원 {b['state']}", "tx": sorted(b["tx"], key=lambda r: r["date"], reverse=True),
                  "filings": sorted(b["filings"], key=lambda r: r["filed"], reverse=True)}
        person["last_activity"] = person["filings"][0]["filed"] if person["filings"] else ""
        unparsed = sum(1 for f in b["filings"] if not f["text"])
        if unparsed:
            person["note"] = f"스캔(종이) 제출 {unparsed}건은 자동 판독 불가 → 원문 PDF 확인"
        for r in person["tx"]:
            feed.append(dict(r, pid=pid, kind="PTR", label="확정"))
        out.append(person)
    log("house members", len(out))
    return out


# ================================================================== OGE 278-T (OCR)
OGE_RX = re.compile(
    r"(?P<desc>[A-Za-z0-9&\.\-,'/%\(\) ]{3,90}?)\s+(?P<type>[pPoO0]urch\w*|[sS]al[eoa]\w*|[eE]xchang\w*)\s+"
    r"(?P<date>\d{1,2}\s*/\s*\d{1,2}\s*/\s*\d{4})\s+(?:\w{2,3}\s+)?(?P<amt>(?:Over\s*)?\$?\s?\d[\d,\. ]{2,}\d(?:\s*[-•·~]\s*\$?\s?\d[\d,\. ]{2,}\d)?)")


def ocr_pdf(blob, max_pages=150):
    """Return (text, used_ocr). Uses the text layer when present, else Tesseract."""
    text = ""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(blob)) as doc:
            n = len(doc.pages)
            text = "\n".join((pg.extract_text() or "") for pg in doc.pages[:max_pages])
    except Exception:
        n = 0
    if len(re.sub(r"\s", "", text)) > 400:
        return text, False
    try:
        from pdf2image import convert_from_bytes
        import pytesseract
        out = []
        for i in range(1, min(n or max_pages, max_pages) + 1):
            imgs = convert_from_bytes(blob, dpi=200, first_page=i, last_page=i)
            if not imgs:
                break
            out.append(pytesseract.image_to_string(imgs[0], config="--psm 6"))
        return "\n".join(out), True
    except Exception as e:
        diag("oge", f"ocr failed: {e}")
        return text, True


def parse_oge_text(text):
    rows = []
    for line in text.splitlines():
        line = line.strip()
        m = OGE_RX.search(line)
        if not m:
            continue
        t = m.group("type").lower()
        kind = "매수" if re.match(r"[po0]urch", t) else "매도" if t.startswith("sal") else "교환"
        try:
            mm, dd, yy = [int(x) for x in re.findall(r"\d+", m.group("date"))[:3]]
            date = dt.date(yy, mm, dd).isoformat()
        except Exception:
            date = ""
        desc = re.sub(r"^\d{1,4}\s+", "", m.group("desc")).strip()
        amt = re.sub(r"\s+", "", m.group("amt"))
        amt = re.sub(r"[•·~]", "-", amt)
        amt = re.sub(r"(?<=\d)\.(?=\d{3}(?:\D|$))", ",", amt)
        amt = re.sub(r"\d[\d,]*", lambda x: f"{int(x.group().replace(',', '')):,}", amt)
        rows.append({"asset": desc[:90], "tx": kind, "date": date, "amount": amt})
    return rows


def build_oge(feed):
    cache = load_json("oge_cache.json", {})
    out = []
    since = (TODAY - dt.timedelta(days=400)).isoformat()
    for o in CFG["officials_oge"]:
        person = {"id": o["id"], "name_ko": o["name_ko"], "group": "official", "tx": [], "filings": []}
        links = []
        try:
            if o["source"] == "whitehouse":
                html = web_get("https://www.whitehouse.gov/disclosures/") or ""
                for u in dict.fromkeys(re.findall(r'href="([^"]+Periodic-Transaction-Report[^"]*\.pdf)"', html)):
                    m = re.search(r"/uploads/(\d{4})/(\d{2})/", u)
                    links.append({"url": u, "title": u.rsplit("/", 1)[-1], "month": f"{m.group(1)}-{m.group(2)}" if m else ""})
                person["entity"] = "백악관 공개 거래보고서 (OGE 278-T)"
            else:
                html = ""
                for base in ("https://extapps2.oge.gov/201/Presiden.nsf/PAS%20Index?OpenView&Start=1&Count=3000",
                             "https://extapps2.oge.gov/201/Presiden.nsf/PAS%20Index?OpenView&Start=1&Count=1000",
                             "https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index?OpenView&Start=1&Count=3000"):
                    html = web_get(base, timeout=90) or ""
                    if o["match"].split(",")[0] in html:
                        break
                if not html:
                    diag("oge", f"{o['id']}: OGE index empty")
                m = re.search(r'href="([^"]+Expand=\d+[^"]*)"[^>]*>\s*<img[^>]+alt="Show details for ' + re.escape(o["match"]), html)
                if m:
                    page = web_get("https://extapps2.oge.gov" + m.group(1).replace("&amp;", "&")) or ""
                    i = page.find(o["match"])
                    j = page.find("Show details for", i + 50)
                    seg = page[i: j if j > 0 else i + 20000]
                    for href, title in re.findall(r'href="([^"]+\$FILE/[^"]+\.pdf)"[^>]*>([^<]*)<', seg, re.I):
                        url = href if href.startswith("http") else "https://extapps2.oge.gov" + href
                        links.append({"url": url, "title": (title or url.rsplit("/", 1)[-1]).strip(), "month": ""})
                else:
                    diag("oge", f"{o['id']}: not found in OGE index (page {len(html)} bytes)")
                person["entity"] = "OGE 공직자 재산공시"
        except Exception as e:
            diag("oge", f"{o['id']}: {e}")
        for lk in links:
            is_ptr = bool(re.search(r"278[\s\-]?T|Periodic", lk["title"] + lk["url"], re.I))
            if lk["month"] and lk["month"] + "-31" < since:
                continue
            c = cache.get(lk["url"]) or {}
            rows = []
            if is_ptr:
                import hashlib
                tf = DATA / "oge_text" / (hashlib.sha1(lk["url"].encode()).hexdigest()[:16] + ".txt")
                if not tf.exists():
                    blob = web_get(lk["url"], binary=True, timeout=120)
                    if blob:
                        text, used = ocr_pdf(blob)
                        tf.parent.mkdir(parents=True, exist_ok=True)
                        tf.write_text(text, encoding="utf-8")
                        c = {"ocr": used, "chars": len(text), "file": tf.name}
                        cache[lk["url"]] = c
                        save_json("oge_cache.json", cache)
                if tf.exists():
                    rows = parse_oge_text(tf.read_text(encoding="utf-8"))
                    c["rows_n"] = len(rows)
                    log("oge", o["id"], lk["title"][:50], len(rows), "rows", "ocr" if c.get("ocr") else "text")
            f = {"title": lk["title"], "url": lk["url"], "ptr": is_ptr, "rows": len(rows), "ocr": c.get("ocr", False), "month": lk["month"]}
            person["filings"].append(f)
            for r in rows:
                person["tx"].append(dict(r, url=lk["url"], ocr=f["ocr"]))
        person["tx"].sort(key=lambda r: r["date"], reverse=True)
        person["last_activity"] = person["tx"][0]["date"] if person["tx"] else ""
        if person["filings"]:
            latest = person["filings"][0]
            feed.append({"date": person["last_activity"] or TODAY.isoformat(), "pid": o["id"], "kind": "278-T",
                         "count": sum(f["rows"] for f in person["filings"]), "url": latest["url"],
                         "title": latest["title"], "label": "OCR" if latest["ocr"] else "확정"})
        out.append(person)
    save_json("oge_cache.json", cache)
    return out


# ================================================================== consensus
def consensus(people):
    periods = {}
    for p in people:
        if p.get("f13") and not p.get("archived"):
            periods[p["f13"]["period"]] = periods.get(p["f13"]["period"], 0) + 1
    if not periods:
        return {}
    period = max(periods, key=lambda k: (periods[k], k))
    agg = {}
    for p in people:
        f = p.get("f13")
        if not f or p.get("archived") or f["period"] != period:
            continue
        for kind in ("new", "add", "cut", "exit"):
            for c in f["changes"][kind]:
                if c["put_call"]:
                    continue
                a = agg.setdefault(c["cusip"], {"cusip": c["cusip"], "name": c["name"], "buy": [], "sell": []})
                (a["buy"] if kind in ("new", "add") else a["sell"]).append({"pid": p["id"], "kind": kind, "value": c["value"], "prev_value": c["prev_value"]})
    rows = list(agg.values())
    for r in rows:
        r["net"] = len(r["buy"]) - len(r["sell"])
    return {"period": period, "rows": sorted(rows, key=lambda r: (-len(r["buy"]), -sum(b["value"] for b in r["buy"])))[:80]}


# ================================================================== main
def main():
    feed, people = [], []
    for fn in (build_sec, build_house, build_oge):
        try:
            people += fn(feed)
        except Exception as e:
            diag(fn.__name__, e)
    cons = consensus(people)
    key = lambda x: x.get("filed") or x.get("date", "")
    feed.sort(key=key, reverse=True)
    cut = (TODAY - dt.timedelta(days=120)).isoformat()
    feed = [f for f in feed if key(f) >= cut][:2000]
    out = {"status": "ok", "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "asof": TODAY.isoformat(), "people": people, "feed": feed, "consensus": cons,
           "cusip_ticker": {c: t for c, t in CUSIP_TICKER.items() if t}, "diag": DIAG}
    save_json("guru.json", out)
    log("wrote guru.json", len(people), "people", len(feed), "feed items", len(DIAG), "diag")


if __name__ == "__main__":
    main()
