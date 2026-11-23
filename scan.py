"""
ApplyRadar weekly scan.
Reads config.json, checks each company's public career site (Workday, Workable, Manatal, SuccessFactors)
or its public LinkedIn job listings,
keeps jobs in Egypt and the Gulf, scores them against the QC and Planning tracks,
and writes results/ (report.html, jobs.json, jobs.csv) plus seen_jobs.json.
Runs free on GitHub Actions every Sunday and Wednesday; can also run on any PC:  pip install requests && python scan.py
"""
import csv, json, os, re, time, datetime, html, random
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
OUT = os.path.join(HERE, "results")
os.makedirs(OUT, exist_ok=True)
HEADERS = {"Accept": "application/json", "Content-Type": "application/json",
           "User-Agent": "ApplyRadar/0.2 (personal weekly job check)"}
PAUSE = 4.0            # seconds between requests (plus a little random jitter)
COMPANY_PAUSE = 10.0   # extra rest between two companies
BACKOFF = [30, 60, 120]  # waits after a 429 before each retry
ERRORS = []
RUN_BUDGET = 50 * 60   # stop starting new companies after 50 min so results are always saved
STOP_AFTER_BLOCKS = 3  # if 3 companies on the same system in a row give up on 429, skip that system this run
RATE_LIMITED = set()   # companies that gave up on 429 in this run


class RateLimited(Exception):
    """Workday still answered 429 after all retries."""


def nap(base=PAUSE):
    time.sleep(base + random.uniform(0, 1.5))


def post_with_backoff(do_post):
    """Run do_post(); on 429 wait (Retry-After if given, else 30/60/120 s) and retry up to 3 times."""
    for wait in BACKOFF + [None]:
        r = do_post()
        if r.status_code != 429:
            return r
        if wait is None:
            raise RateLimited("429 Too Many Requests after 3 retries")
        ra = r.headers.get("Retry-After", "")
        delay = int(ra) if ra.isdigit() else wait
        print(f"  429 received, waiting {delay}s", flush=True)
        time.sleep(min(delay, 300))


def get_with_backoff(url, **kw):
    """GET with the same 429 backoff as Workday requests."""
    kw.setdefault("timeout", 30)
    return post_with_backoff(lambda: requests.get(url, **kw))


SESSIONS = {}


def wd_session(c):
    """Some Workday sites (e.g. Lilly, Takeda) answer 422 unless the request carries the site's
    cookies and CSRF token. Open the public career page once to get them."""
    key = c["tenant"] + c["site"]
    if key not in SESSIONS:
        s = requests.Session()
        s.headers.update(HEADERS)
        try:
            s.get(f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com/{c['site']}", timeout=30,
                  headers={"Accept": "text/html"})
            tok = s.cookies.get("CALYPSO_CSRF_TOKEN")
            if tok:
                s.headers["X-CALYPSO-CSRF-TOKEN"] = tok
        except Exception:
            pass
        SESSIONS[key] = s
    return SESSIONS[key]


def fetch(c, word):
    url = f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com/wday/cxs/{c['tenant']}/{c['site']}/jobs"
    out, offset = [], 0
    while offset < 200:
        body = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": word}
        r = post_with_backoff(lambda: requests.post(url, headers=HEADERS, timeout=30, json=body))
        if r.status_code == 422:  # retry with the site's cookies + CSRF token
            r = post_with_backoff(lambda: wd_session(c).post(url, timeout=30, json=body))
        r.raise_for_status()
        posts = r.json().get("jobPostings", [])
        out += posts
        if len(posts) < 20:
            break
        offset += 20
        nap()
    return out


def workday_jobs(c):
    """Normalized jobs from a Workday site, searched by each location word."""
    for word in CFG["search_words"]:
        try:
            posts = fetch(c, word)
        except RateLimited:
            # Workday keeps refusing: stop hammering this company, move to the next one
            RATE_LIMITED.add(c["name"])
            ERRORS.append(f"{c['name']} / {word}: 429 Too Many Requests (gave up after 3 retries, skipped the rest of this company)")
            return
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code in (400, 404, 422):  # wrong tenant/site: report once, skip the company
                ERRORS.append(f"{c['name']}: career site not found ({code}) - check tenant/site")
                return
            ERRORS.append(f"{c['name']} / {word}: {e}")
            posts = []
        except Exception as e:
            ERRORS.append(f"{c['name']} / {word}: {e}")
            posts = []
        for p in posts:
            path = p.get("externalPath", "") or ""
            yield {"id": f"{c['tenant']}-{path.rsplit('_', 1)[-1]}", "title": p.get("title", ""),
                   "location": (p.get("locationsText", "") or "") + " " + path,
                   "display_loc": p.get("locationsText", "") or "", "posted": p.get("postedOn", ""),
                   "url": f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com/{c['site']}{path}"}
        nap()


def workable_jobs(c):
    """Workable public widget API: one call returns every open job."""
    r = requests.get(f"https://apply.workable.com/api/v1/widget/accounts/{c['account']}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    for j in r.json().get("jobs", []):
        locs = j.get("locations") or [{"city": j.get("city", ""), "country": j.get("country", "")}]
        loc = "; ".join(", ".join(x for x in (l.get("city"), l.get("country")) if x) for l in locs)
        yield {"id": f"{c['account']}-{j.get('shortcode')}", "title": j.get("title", ""), "location": loc,
               "display_loc": loc, "posted": j.get("published_on", ""), "url": j.get("url", "")}


def manatal_jobs(c):
    """Manatal careers-page.com API, paginated."""
    url = f"https://www.careers-page.com/api/v1.0/c/{c['slug']}/jobs/"
    pages = 0
    while url and pages < 30:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        d = r.json()
        for j in d.get("results", []):
            loc = j.get("location_display") or ", ".join(x for x in (j.get("city"), j.get("country")) if x)
            yield {"id": f"{c['slug'][:20]}-{j.get('hash') or j.get('id')}", "title": j.get("position_name", ""),
                   "location": loc, "display_loc": loc, "posted": "",
                   "url": f"https://www.careers-page.com/{c['slug']}/job/{j.get('hash')}"}
        url, pages = d.get("next"), pages + 1
        time.sleep(PAUSE)


BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}


def _text(fragment):
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment or "")).strip()


def successfactors_jobs(c):
    """SAP SuccessFactors career site (RMK), e.g. careers.julphar.net: reads the public search pages."""
    base = c["base"].rstrip("/")
    seen_ids, start = set(), 0
    while start < 1000:
        try:
            r = get_with_backoff(f"{base}/search/", headers=BROWSER, params={
                "q": "", "sortColumn": "referencedate", "sortDirection": "desc", "startrow": start})
        except RateLimited:
            RATE_LIMITED.add(c["name"])
            ERRORS.append(f"{c['name']}: 429 Too Many Requests (gave up after 3 retries)")
            return
        r.raise_for_status()
        rows = re.findall(r'<tr[^>]*class="[^"]*data-row[^"]*"[^>]*>(.*?)</tr>', r.text, re.S)
        added = 0
        for row in rows:
            a = re.search(r'<a[^>]*href="(/job/[^"]+)"[^>]*>(.*?)</a>', row, re.S)
            if not a:
                continue
            path = html.unescape(a.group(1))
            jid = path.rstrip("/").rsplit("/", 1)[-1]
            if jid in seen_ids:
                continue
            seen_ids.add(jid)
            added += 1
            loc = re.search(r'class="[^"]*jobLocation[^"]*"[^>]*>(.*?)</span>', row, re.S)
            date = re.search(r'class="[^"]*jobDate[^"]*"[^>]*>(.*?)</span>', row, re.S)
            loc = _text(loc.group(1)) if loc else ""
            yield {"id": f"sf-{c['name'][:12].lower().replace(' ', '')}-{jid}", "title": _text(a.group(2)),
                   "location": loc + " " + path, "display_loc": loc,
                   "posted": _text(date.group(1)) if date else "", "url": base + path}
        if not added:
            break
        start += added
        nap()


LI_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"


def linkedin_jobs(c):
    """LinkedIn public job search (no login), for companies whose own career site can't be read.
    Searches the company name in each location and keeps only cards posted by that company."""
    match = [m.lower() for m in c.get("match", [c["name"]])]
    for loc in c.get("locations", ["Saudi Arabia", "United Arab Emirates"]):
        start = 0
        while start < 100:
            try:
                r = get_with_backoff(LI_URL, headers=BROWSER, params={
                    "keywords": c.get("query", c["name"]), "location": loc, "start": start})
            except RateLimited:
                RATE_LIMITED.add(c["name"])
                ERRORS.append(f"{c['name']} / LinkedIn {loc}: 429 Too Many Requests (gave up after 3 retries, "
                              "skipped the rest of this company)")
                return
            if r.status_code in (400, 404):
                break
            r.raise_for_status()
            cards = re.findall(r'<li>(.*?)</li>', r.text, re.S)
            if not cards:
                break
            for card in cards:
                company = re.search(r'base-search-card__subtitle[^>]*>(.*?)</h4>', card, re.S)
                company = _text(company.group(1)) if company else ""
                if not any(m in company.lower() for m in match):
                    continue
                jid = re.search(r'jobPosting:(\d+)', card)
                title = re.search(r'base-search-card__title[^>]*>(.*?)</h3>', card, re.S)
                where = re.search(r'job-search-card__location[^>]*>(.*?)</span>', card, re.S)
                when = re.search(r'<time[^>]*datetime="([^"]+)"', card)
                if not (jid and title):
                    continue
                where = _text(where.group(1)) if where else loc
                yield {"id": f"li-{jid.group(1)}", "title": _text(title.group(1)), "location": where,
                       "display_loc": where, "posted": when.group(1) if when else "",
                       "url": f"https://www.linkedin.com/jobs/view/{jid.group(1)}"}
            start += len(cards)
            nap()
        nap()


def age_days(posted):
    """Days since posting, from an ISO date or Workday text ("Posted 3 Days Ago"). None if unknown."""
    p = (posted or "").strip().lower()
    if not p:
        return None
    try:
        d = datetime.date.fromisoformat(p[:10])
        return (datetime.date.today() - d).days
    except ValueError:
        pass
    for fmt in ("%b %d, %Y", "%d %b %Y", "%d/%m/%Y", "%B %d, %Y"):  # SuccessFactors dates, e.g. "Nov 20, 2026"
        try:
            return (datetime.date.today() - datetime.datetime.strptime(posted.strip(), fmt).date()).days
        except ValueError:
            pass
    if "today" in p:
        return 0
    if "yesterday" in p:
        return 1
    digits = "".join(ch for ch in p if ch.isdigit())
    if digits:
        n = int(digits)
        return n + 1 if "+" in p else n
    return None


SOURCES = {"workday": workday_jobs, "workable": workable_jobs, "manatal": manatal_jobs,
           "successfactors": successfactors_jobs, "linkedin": linkedin_jobs}


def region_of(text):
    t = " " + (text or "").lower()
    for reg, words in CFG["regions"].items():
        if any(w in t for w in words):
            return reg
    return None


def loc_state(text, region):
    """Location tier and score change: hidden / east (-10) / preferred (0) / cairo (-5) / gulf (0)."""
    L = CFG["locations"]
    pts = L.get("points", {})
    t = (text or "").lower()
    if any(w in t for w in L["excluded"]):
        return "hidden", 0
    if region == "Gulf":
        return "gulf", pts.get("gulf", 0)
    if any(w in t for w in L.get("east", [])):
        return "east", pts.get("east", -10)
    if any(w in t for w in L.get("preferred", [])):
        return "preferred", pts.get("preferred", 0)
    return "cairo", pts.get("cairo_or_unspecified", -5)  # Cairo, New Cairo, New Capital or just "Egypt"


def score(title):
    t = " " + title.lower() + " "
    best = (0, None)
    for tid, tr in CFG["tracks"].items():
        if any(w in t for w in tr["exclude_titles"]):
            continue
        if not any(w in t for w in tr["keywords"]):
            continue
        s = 60
        if any(w in t for w in tr["senior"]):
            s += 20
        elif any(w in t for w in tr.get("mid", [])):
            s += 10
        if any(w in t for w in CFG.get("junior_words", [])):
            s -= 20
        if any(w in t for w in tr["bonus"]):
            s += 10
        if tid == "qc":
            s += 5  # primary track
        best = max(best, (min(s, 100), tid))
    return best


def main():
    seen_path = os.path.join(HERE, "seen_jobs.json")
    seen = set(json.load(open(seen_path))) if os.path.exists(seen_path) else set()
    jobs, errors = {}, ERRORS
    skip = [x.lower() for x in CFG.get("excluded_companies", [])]
    started, streak, blocked = time.time(), {}, {}
    for c in CFG["companies"]:
        if c["name"].lower() in skip:
            continue
        ats = c.get("ats", "workday")
        if time.time() - started > RUN_BUDGET:
            errors.append(f"Time limit: {c['name']} and the companies after it were not checked this run")
            break
        if ats in blocked:
            blocked[ats].append(c["name"])
            continue
        try:
            for j in SOURCES[ats](c):
                reg = region_of(j["location"])
                if not reg:
                    continue
                key = j["id"]
                fit, track = score(j["title"])
                age = age_days(j["posted"])
                tags = []
                if age is not None and age > CFG.get("stale_after_days", 45) and fit:
                    fit = max(fit - 25, 1)  # old posting: probably filled or evergreen
                loc, loc_pts = loc_state(j["location"], reg)
                if loc == "east":
                    tags.append("East")
                pen = CFG.get("company_penalties", {}).get(c["name"])
                if pen:
                    tags.append(pen["tag"])
                if fit:
                    fit = max(fit + loc_pts + (pen["points"] if pen else 0), 1)
                jobs[key] = {
                    "id": key, "key": key, "company": c["name"], "title": j["title"],
                    "location": j["display_loc"], "region": reg, "posted": j["posted"],
                    "url": j["url"], "source": ats, "fit": fit, "track": track, "age_days": age,
                    "loc": loc, "tags": tags, "new": key not in seen,
                }
        except Exception as e:
            errors.append(f"{c['name']} ({ats}): {e}")
        streak[ats] = streak.get(ats, 0) + 1 if c["name"] in RATE_LIMITED else 0
        if streak[ats] >= STOP_AFTER_BLOCKS:
            blocked[ats] = []
        print(f"{c['name']}: done", flush=True)
        nap(COMPANY_PAUSE)
    for ats, names in blocked.items():
        if names:
            errors.append(f"{ats}: {STOP_AFTER_BLOCKS} companies in a row answered 429, so these were skipped "
                          f"this run: {', '.join(names)}")

    rows = [j for j in jobs.values() if j["loc"] != "hidden"]
    rows.sort(key=lambda j: (-j["fit"], j["age_days"] if j["age_days"] is not None else 999, j["company"]))
    matched = [j for j in rows if j["track"]]
    json.dump({"scanned_at": datetime.datetime.utcnow().isoformat() + "Z", "jobs": rows, "errors": errors},
              open(os.path.join(OUT, "jobs.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(os.path.join(OUT, "jobs.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["fit", "age_days", "track", "new", "company", "title", "location", "region", "loc", "posted", "url"],
                           extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    write_html(rows, matched, errors, len(jobs) - len(rows))
    json.dump(sorted(seen | set(jobs)), open(seen_path, "w"))
    print(f"{len(rows)} jobs kept · {len(matched)} match a track · {len(errors)} errors")


def write_html(rows, matched, errors, hidden):
    now = datetime.datetime.utcnow().strftime("%d %b %Y %H:%M UTC")
    def row(j):
        tags = ('<b class="n">NEW</b> ' if j["new"] else "") + " ".join(f'<i>{html.escape(t)}</i>' for t in j.get("tags", []))
        tr = CFG["tracks"].get(j["track"], {}).get("label", "–")
        return (f'<tr><td>{j["fit"] or "–"}</td><td><a href="{html.escape(j["url"])}">{html.escape(j["title"])}</a> {tags}</td>'
                f'<td>{tr}</td><td>{html.escape(j["company"])}</td><td>{html.escape(j["location"])}</td><td>{html.escape(j["posted"])}</td></tr>')
    other = [j for j in rows if not j["track"]]
    head = "<tr><th>Fit</th><th>Role</th><th>Track</th><th>Company</th><th>Location</th><th>Posted</th></tr>"
    err = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
    doc = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ApplyRadar weekly</title>
<style>body{{font-family:system-ui,sans-serif;background:#F7F5F0;color:#1E2433;max-width:1000px;margin:24px auto;padding:0 16px}}
h1,h2{{color:#1B2B4B}}table{{width:100%;border-collapse:collapse;background:#fff;margin-bottom:28px}}
td,th{{padding:8px 10px;border-bottom:1px solid #E6E1D6;text-align:left;font-size:14px}}th{{background:#1B2B4B;color:#fff}}
.n{{background:#E3A03A;color:#1B2B4B;border-radius:5px;padding:1px 5px;font-size:11px}}a{{color:#1B2B4B}}</style>
<h1>ApplyRadar weekly scan</h1><p>{now} · {len(rows)} roles kept · {hidden} hidden by location</p>
<h2>Matching your tracks ({len(matched)})</h2><table>{head}{''.join(map(row, matched))}</table>
<h2>Other roles ({len(other)})</h2><table>{head}{''.join(map(row, other))}</table>
{'<h3>Errors</h3><ul>' + err + '</ul>' if err else ''}"""
    open(os.path.join(OUT, "report.html"), "w", encoding="utf-8").write(doc)


if __name__ == "__main__":
    main()
