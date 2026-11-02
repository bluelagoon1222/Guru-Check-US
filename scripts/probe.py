#!/usr/bin/env python3
"""
Access + format probe for Guru-Check-US.
Checks every planned data source from GitHub Actions and stores samples in probe/result.json
so the real parsers can be written against actual responses.
Needs secret SEC_USER_AGENT (e.g. "SJ Market Desk you@example.com").
"""
import datetime as dt
import io
import json
import os
import re
import time
import zipfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "probe" / "result.json"
CFG = json.loads((ROOT / "config" / "people.json").read_text(encoding="utf-8"))
SEC_UA = os.environ.get("SEC_USER_AGENT", "").strip()
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
YEAR = dt.date.today().year
R = {"run_at_utc": dt.datetime.utcnow().isoformat(timespec="seconds"), "checks": []}


def rec(name, ok, **kw):
    kw.update({"name": name, "ok": ok})
    R["checks"].append(kw)
    print(("OK   " if ok else "FAIL ") + name, {k: (v if not isinstance(v, str) else v[:120]) for k, v in kw.items() if k not in ("name", "ok", "sample")})


def sec_get(url, as_json=True):
    time.sleep(0.15)
    r = requests.get(url, headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip"}, timeout=60)
    r.raise_for_status()
    return r.json() if as_json else r.text


# ------------------------------------------------------------------ SEC
def probe_sec():
    if "@" not in SEC_UA:
        rec("sec_user_agent", False, error="secret SEC_USER_AGENT missing or without email")
        return
    berk_13f = None
    for grp in ("investors_13f", "insiders_form4"):
        for p in CFG[grp]:
            try:
                js = sec_get(f"https://data.sec.gov/submissions/CIK{p['cik']:010d}.json")
                name = js.get("name", "")
                rc = js["filings"]["recent"]
                forms = rc["form"]
                cnt = {}
                for f in forms[:400]:
                    cnt[f] = cnt.get(f, 0) + 1
                last13f = next(((rc["accessionNumber"][i], rc["filingDate"][i]) for i, f in enumerate(forms)
                                if f in ("13F-HR", "13F-HR/A")), None)
                last4 = next((rc["filingDate"][i] for i, f in enumerate(forms) if f == "4"), None)
                rec(f"sec_sub_{p['id']}", p["expect"].upper() in name.upper(), entity=name,
                    last_13f=last13f, last_form4=last4,
                    forms_top={k: v for k, v in sorted(cnt.items(), key=lambda x: -x[1])[:8]})
                if p["id"] == "buffett" and last13f:
                    berk_13f = last13f[0]
                if p["id"] == "musk":
                    i4 = next((i for i, f in enumerate(forms) if f == "4"), None)
                    if i4 is not None:
                        R["musk_form4"] = {"accn": rc["accessionNumber"][i4], "doc": rc["primaryDocument"][i4]}
            except Exception as e:
                rec(f"sec_sub_{p['id']}", False, error=str(e)[:200])
    # 13F information table for Berkshire
    if berk_13f:
        try:
            nd = berk_13f.replace("-", "")
            idx = sec_get(f"https://www.sec.gov/Archives/edgar/data/1067983/{nd}/index.json")
            files = [it["name"] for it in idx["directory"]["item"]]
            xmls = [f for f in files if f.lower().endswith(".xml") and "primary_doc" not in f.lower()]
            txt = sec_get(f"https://www.sec.gov/Archives/edgar/data/1067983/{nd}/{xmls[0]}", as_json=False) if xmls else ""
            rows = re.findall(r"<(?:\w+:)?infoTable>(.*?)</(?:\w+:)?infoTable>", txt, re.S)
            first = rows[0] if rows else txt[:500]
            rec("sec_13f_infotable", bool(rows), files=files, rows=len(rows), sample=re.sub(r"\s+", " ", first)[:700])
        except Exception as e:
            rec("sec_13f_infotable", False, error=str(e)[:200])
    # Form 4 XML (Musk)
    m4 = R.get("musk_form4")
    if m4:
        try:
            nd = m4["accn"].replace("-", "")
            doc = m4["doc"].split("/")[-1]
            txt = sec_get(f"https://www.sec.gov/Archives/edgar/data/1494730/{nd}/{doc}", as_json=False)
            rec("sec_form4_xml", "<ownershipDocument" in txt or "issuerTradingSymbol" in txt, doc=m4["doc"],
                sample=re.sub(r"\s+", " ", txt)[:600])
        except Exception as e:
            rec("sec_form4_xml", False, error=str(e)[:200])


# ------------------------------------------------------------------ OpenFIGI (CUSIP -> ticker)
def probe_figi():
    try:
        jobs = [{"idType": "ID_CUSIP", "idValue": c} for c in ("037833100", "060505104", "191216100")]
        r = requests.post("https://api.openfigi.com/v3/mapping", json=jobs,
                          headers={"Content-Type": "application/json"}, timeout=30)
        js = r.json()
        tick = [((x.get("data") or [{}])[0]).get("ticker") for x in js]
        rec("openfigi", r.status_code == 200 and all(tick), status=r.status_code, tickers=tick)
    except Exception as e:
        rec("openfigi", False, error=str(e)[:200])


# ------------------------------------------------------------------ House (STOCK Act PTRs)
def probe_house():
    try:
        url = f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.zip"
        r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=60)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = z.namelist()
        xmlname = next(n for n in names if n.lower().endswith(".xml"))
        xml = z.read(xmlname).decode("utf-8", "replace")
        members = re.findall(r"<Member>(.*?)</Member>", xml, re.S)
        def f(blk, tag):
            m = re.search(rf"<{tag}>(.*?)</{tag}>", blk, re.S)
            return m.group(1).strip() if m else ""
        ptr = [m for m in members if f(m, "FilingType") == "P"]
        pel = [m for m in ptr if f(m, "Last").lower() == "pelosi"]
        rec("house_index", bool(members), zip_files=names, members=len(members), ptr=len(ptr), pelosi_ptr=len(pel),
            sample=re.sub(r"\s+", " ", (pel or ptr or members)[-1])[:400] if members else "")
        target = (pel or ptr)[-1] if (pel or ptr) else None
        if target:
            doc = f(target, "DocID")
            purl = f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{YEAR}/{doc}.pdf"
            pr = requests.get(purl, headers={"User-Agent": BROWSER_UA}, timeout=60)
            text = ""
            try:
                import pdfplumber
                with pdfplumber.open(io.BytesIO(pr.content)) as pdf:
                    text = "\n".join((pg.extract_text() or "") for pg in pdf.pages[:2])
            except Exception as e:
                text = f"pdf error {e}"
            rec("house_ptr_pdf", pr.status_code == 200 and len(text) > 50, url=purl, status=pr.status_code,
                chars=len(text), sample=text[:1500])
    except Exception as e:
        rec("house_index", False, error=str(e)[:200])


# ------------------------------------------------------------------ Senate eFD
def probe_senate():
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": BROWSER_UA})
        home = s.get("https://efdsearch.senate.gov/search/home/", timeout=30)
        tok = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', home.text)
        if not tok:
            rec("senate_efd", False, status=home.status_code, error="no csrf token", sample=home.text[:300])
            return
        s.post("https://efdsearch.senate.gov/search/home/",
               data={"csrfmiddlewaretoken": tok.group(1), "prohibition_agreement": "1"},
               headers={"Referer": "https://efdsearch.senate.gov/search/home/"}, timeout=30)
        csrf = s.cookies.get("csrftoken") or tok.group(1)
        start = (dt.date.today() - dt.timedelta(days=60)).strftime("%m/%d/%Y 00:00:00")
        data = {"start": "0", "length": "25", "report_types": "[11]", "filer_types": "[]",
                "submitted_start_date": start, "submitted_end_date": "", "candidate_state": "",
                "senator_state": "", "office_id": "", "first_name": "", "last_name": "",
                "csrfmiddlewaretoken": csrf}
        r = s.post("https://efdsearch.senate.gov/search/report/data/", data=data,
                   headers={"Referer": "https://efdsearch.senate.gov/search/", "X-CSRFToken": csrf}, timeout=30)
        js = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        rec("senate_efd", bool(js.get("data")), status=r.status_code, total=js.get("recordsTotal"),
            sample=json.dumps((js.get("data") or [])[:2])[:600] if js else r.text[:300])
    except Exception as e:
        rec("senate_efd", False, error=str(e)[:200])


# ------------------------------------------------------------------ OGE (President / nominees)
def probe_oge():
    pages = ["https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index?OpenView&Start=1&Count=3000",
             "https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index?OpenView"]
    html = ""
    for u in pages:
        try:
            r = requests.get(u, headers={"User-Agent": BROWSER_UA}, timeout=60)
            if r.status_code == 200 and len(r.text) > 1000:
                html = r.text
                rec("oge_index", True, url=u, bytes=len(html))
                break
            rec("oge_index_try", False, url=u, status=r.status_code, bytes=len(r.text))
        except Exception as e:
            rec("oge_index_try", False, url=u, error=str(e)[:200])
    if not html:
        return
    links = re.findall(r'href="([^"]+\.pdf)"', html, re.I)
    found = {}
    for o in CFG["officials_oge"]:
        hits = [l for l in links if o["match"].lower() in l.lower()]
        found[o["id"]] = hits[:6]
    rec("oge_links", any(found.values()), counts={k: len(v) for k, v in found.items()}, sample=json.dumps(found)[:900])
    tr = [l for l in found.get("trump", []) if "278" in l]
    if tr:
        url = tr[0] if tr[0].startswith("http") else "https://extapps2.oge.gov" + tr[0]
        try:
            pr = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=90)
            import pdfplumber
            with pdfplumber.open(io.BytesIO(pr.content)) as pdf:
                pages_n = len(pdf.pages)
                text = pdf.pages[min(1, pages_n - 1)].extract_text() or ""
            ocr = ""
            if len(text.strip()) < 40:
                try:
                    from pdf2image import convert_from_bytes
                    import pytesseract
                    img = convert_from_bytes(pr.content, dpi=200, first_page=2, last_page=2)[0]
                    ocr = pytesseract.image_to_string(img)
                except Exception as e:
                    ocr = f"ocr error {e}"
            rec("oge_trump_pdf", pr.status_code == 200, url=url, pages=pages_n, text_layer=len(text.strip()),
                sample=(text or ocr)[:1500])
        except Exception as e:
            rec("oge_trump_pdf", False, url=url, error=str(e)[:200])


# ------------------------------------------------------------------ ARK daily holdings
def probe_ark():
    url = "https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_INNOVATION_ETF_ARKK_HOLDINGS.csv"
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=30)
        rec("ark_csv", r.status_code == 200 and "ticker" in r.text[:500].lower(), status=r.status_code, sample=r.text[:400])
    except Exception as e:
        rec("ark_csv", False, error=str(e)[:200])


# ------------------------------------------------------------------ round 2: fill the gaps
def probe_round2():
    # (a) Jensen Huang CIK from NVIDIA's insider ownership page
    try:
        txt = sec_get("https://www.sec.gov/cgi-bin/own-disp?action=getissuer&CIK=0001045810", as_json=False)
        hits = re.findall(r'CIK=(\d{10})[^>]*>([^<]*HUANG[^<]*)<', txt, re.I)
        rec("r2_huang_cik", bool(hits), hits=hits[:5], bytes=len(txt), sample=txt[txt.upper().find("HUANG")-300:txt.upper().find("HUANG")+300] if "HUANG" in txt.upper() else txt[:500])
    except Exception as e:
        rec("r2_huang_cik", False, error=str(e)[:200])
    # (b) OGE index: where do the names appear and what do the links look like
    try:
        r = requests.get("https://extapps2.oge.gov/201/Presiden.nsf/PAS+Index?OpenView&Start=1&Count=3000",
                         headers={"User-Agent": BROWSER_UA}, timeout=60)
        h = r.text
        ctx = {}
        for nm in ("Trump", "Warsh", "Bessent", "Lutnick"):
            i = h.find(nm)
            ctx[nm] = h[max(0, i - 700): i + 500] if i >= 0 else None
        hrefs = re.findall(r'href="([^"]+)"', h)[:40]
        rec("r2_oge_structure", any(ctx.values()), bytes=len(h), hrefs=hrefs, sample=json.dumps(ctx)[:6000])
    except Exception as e:
        rec("r2_oge_structure", False, error=str(e)[:200])
    # (c) White House PTR listing as a second source for the President
    for u in ("https://www.whitehouse.gov/disclosures/", "https://www.whitehouse.gov/disclosures/financial-disclosures/"):
        try:
            r = requests.get(u, headers={"User-Agent": BROWSER_UA}, timeout=30)
            pdfs = re.findall(r'href="([^"]+Periodic-Transaction-Report[^"]*\.pdf)"', r.text)
            rec("r2_whitehouse", r.status_code == 200 and bool(pdfs), url=u, status=r.status_code, pdfs=pdfs[:6])
        except Exception as e:
            rec("r2_whitehouse", False, url=u, error=str(e)[:200])
    # (d) Senate eFD with fuller browser headers
    try:
        s = requests.Session()
        s.headers.update({"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                          "Accept-Language": "en-US,en;q=0.9"})
        r = s.get("https://efdsearch.senate.gov/search/home/", timeout=30)
        rec("r2_senate_home", r.status_code == 200, status=r.status_code, sample=r.text[:300])
    except Exception as e:
        rec("r2_senate_home", False, error=str(e)[:200])
    # (e) ARK: find the current holdings CSV link on the fund page
    for u in ("https://www.ark-funds.com/funds/arkk", "https://ark-funds.com/funds/arkk/"):
        try:
            r = requests.get(u, headers={"User-Agent": BROWSER_UA}, timeout=30)
            csvs = re.findall(r'(https?://[^"\']+\.csv)', r.text)
            rec("r2_ark_page", bool(csvs), url=u, status=r.status_code, csvs=csvs[:6])
            if csvs:
                c = requests.get(csvs[0], headers={"User-Agent": BROWSER_UA}, timeout=30)
                rec("r2_ark_csv", c.status_code == 200, url=csvs[0], status=c.status_code, sample=c.text[:400])
                break
        except Exception as e:
            rec("r2_ark_page", False, url=u, error=str(e)[:200])


if __name__ == "__main__":
    for fn in (probe_round2,):
        try:
            fn()
        except Exception as e:
            rec(fn.__name__, False, error=str(e)[:200])
    R.pop("musk_form4", None)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(R, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", OUT)
