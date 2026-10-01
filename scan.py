"""
ApplyRadar weekly scan.
Reads config.json, checks each company's public career site (Workday, Workable, Manatal),
keeps jobs in Egypt and the Gulf, scores them against the QC and Planning tracks,
and writes results/ (report.html, jobs.json, jobs.csv) plus seen_jobs.json.
Runs free on GitHub Actions every Tuesday; can also run on any PC:  pip install requests && python scan.py
"""
import csv, json, os, time, datetime, html
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
OUT = os.path.join(HERE, "results")
os.makedirs(OUT, exist_ok=True)
HEADERS = {"Accept": "application/json", "Content-Type": "application/json",
           "User-Agent": "ApplyRadar/0.2 (personal weekly job check)"}
PAUSE = 1.5
ERRORS = []


def fetch(c, word):
    url = f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com/wday/cxs/{c['tenant']}/{c['site']}/jobs"
    out, offset = [], 0
    while offset < 200:
        r = requests.post(url, headers=HEADERS, timeout=30,
                          json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": word})
        r.raise_for_status()
        posts = r.json().get("jobPostings", [])
        out += posts
        if len(posts) < 20:
            break
        offset += 20
        time.sleep(PAUSE)
    return out


def workday_jobs(c):
    """Normalized jobs from a Workday site, searched by each location word."""
    for word in CFG["search_words"]:
        try:
            posts = fetch(c, word)
        except Exception as e:
            ERRORS.append(f"{c['name']} / {word}: {e}")
            posts = []
        for p in posts:
            path = p.get("externalPath", "") or ""
            yield {"id": f"{c['tenant']}-{path.rsplit('_', 1)[-1]}", "title": p.get("title", ""),
                   "location": (p.get("locationsText", "") or "") + " " + path,
                   "display_loc": p.get("locationsText", "") or "", "posted": p.get("postedOn", ""),
                   "url": f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com/{c['site']}{path}"}
        time.sleep(PAUSE)


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
    if "today" in p:
        return 0
    if "yesterday" in p:
        return 1
    digits = "".join(ch for ch in p if ch.isdigit())
    if digits:
        n = int(digits)
        return n + 1 if "+" in p else n
    return None


SOURCES = {"workday": workday_jobs, "workable": workable_jobs, "manatal": manatal_jobs}


def region_of(text):
    t = (text or "").lower()
    for reg, words in CFG["regions"].items():
        if any(w in t for w in words):
            return reg
    return None


def loc_state(text):
    t = (text or "").lower()
    if any(w in t for w in CFG["locations"]["excluded"]):
        return "hidden"
    if any(w in t for w in CFG["locations"]["stretch"]):
        return "stretch"
    return "ok"


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
    for c in CFG["companies"]:
        if c["name"].lower() in skip:
            continue
        ats = c.get("ats", "workday")
        try:
            for j in SOURCES[ats](c):
                reg = region_of(j["location"])
                if not reg:
                    continue
                key = j["id"]
                fit, track = score(j["title"])
                age = age_days(j["posted"])
                if age is not None and age > CFG.get("stale_after_days", 45) and fit:
                    fit = max(fit - 25, 1)  # old posting: probably filled or evergreen
                jobs[key] = {
                    "id": key, "key": key, "company": c["name"], "title": j["title"],
                    "location": j["display_loc"], "region": reg, "posted": j["posted"],
                    "url": j["url"], "source": ats, "fit": fit, "track": track, "age_days": age,
                    "loc": loc_state(j["location"]), "new": key not in seen,
                }
        except Exception as e:
            errors.append(f"{c['name']} ({ats}): {e}")
        print(f"{c['name']}: done")

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
        tags = ('<b class="n">NEW</b> ' if j["new"] else "") + ('<i>stretch</i>' if j["loc"] == "stretch" else "")
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
