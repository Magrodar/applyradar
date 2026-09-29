"""
ApplyRadar weekly scan.
Reads config.json, checks each company's public Workday career site,
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
        if any(w in t for w in tr["bonus"]):
            s += 10
        if tid == "qc":
            s += 5  # primary track
        best = max(best, (min(s, 100), tid))
    return best


def main():
    seen_path = os.path.join(HERE, "seen_jobs.json")
    seen = set(json.load(open(seen_path))) if os.path.exists(seen_path) else set()
    jobs, errors = {}, []
    for c in CFG["companies"]:
        for word in CFG["search_words"]:
            try:
                for p in fetch(c, word):
                    loc = p.get("locationsText", "") or ""
                    path = p.get("externalPath", "") or ""
                    reg = region_of(loc) or region_of(path)
                    if not reg:
                        continue
                    key = f"{c['tenant']}:{path}"
                    fit, track = score(p.get("title", ""))
                    jobs[key] = {
                        "key": key, "company": c["name"], "title": p.get("title", ""),
                        "location": loc, "region": reg, "posted": p.get("postedOn", ""),
                        "url": f"https://{c['tenant']}.{c['wd']}.myworkdayjobs.com/{c['site']}{path}",
                        "fit": fit, "track": track, "loc": loc_state(loc + " " + path),
                        "new": key not in seen,
                    }
            except Exception as e:
                errors.append(f"{c['name']} / {word}: {e}")
            time.sleep(PAUSE)
        print(f"{c['name']}: done")

    rows = [j for j in jobs.values() if j["loc"] != "hidden"]
    rows.sort(key=lambda j: (-j["fit"], not j["new"], j["company"]))
    matched = [j for j in rows if j["track"]]
    json.dump({"scanned_at": datetime.datetime.utcnow().isoformat() + "Z", "jobs": rows, "errors": errors},
              open(os.path.join(OUT, "jobs.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(os.path.join(OUT, "jobs.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["fit", "track", "new", "company", "title", "location", "region", "loc", "posted", "url"],
                           extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    write_html(rows, matched, errors, len(jobs) - len(rows))
    write_summary(rows, matched, errors)
    json.dump(sorted(seen | set(jobs)), open(seen_path, "w"))
    print(f"{len(rows)} jobs kept · {len(matched)} match a track · {len(errors)} errors")


def write_summary(rows, matched, errors):
    """Markdown body for the weekly GitHub issue (GitHub emails it to you)."""
    new_matches = [j for j in matched if j["new"]]
    lines = [f"**{len(new_matches)} new matching roles** this week · {len(matched)} matching in total · {len(rows)} roles in Egypt and the Gulf", ""]
    if new_matches:
        lines += ["| Fit | Role | Company | Location |", "|---|---|---|---|"]
        for j in new_matches[:40]:
            tag = " (stretch)" if j["loc"] == "stretch" else ""
            lines.append(f"| {j['fit']} | [{j['title']}]({j['url']}) | {j['company']} | {j['location']}{tag} |")
    else:
        lines.append("No new QC or Planning roles at the 8 Workday companies this week.")
    if errors:
        lines += ["", f"<details><summary>{len(errors)} errors</summary>", ""] + [f"- {e}" for e in errors[:30]] + ["</details>"]
    lines += ["", "Full report: `results/report.html` in the repo."]
    open(os.path.join(OUT, "summary.md"), "w", encoding="utf-8").write("\n".join(lines))


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
