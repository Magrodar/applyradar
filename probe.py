"""Temporary probe: run the new SuccessFactors/LinkedIn sources once and print what each returns."""
import time, scan
t0 = time.time()
def log(*a): print(f"[{int(time.time()-t0):4d}s]", *a, flush=True)
for c in scan.CFG["companies"]:
    if c.get("ats") not in ("successfactors", "linkedin"):
        continue
    try:
        jobs = list(scan.SOURCES[c["ats"]](c))
        gulf = [j for j in jobs if scan.region_of(j["location"])]
        log(f"PROBE {c['name']} ({c['ats']}): {len(jobs)} jobs, {len(gulf)} in region")
        for j in gulf[:2]:
            log("    ", j["title"], "|", j["display_loc"], "|", j["posted"])
    except Exception as e:
        log(f"PROBE {c['name']} ({c['ats']}): ERROR {e}")
    scan.nap(scan.COMPANY_PAUSE)
log("RATE_LIMITED:", sorted(scan.RATE_LIMITED))
