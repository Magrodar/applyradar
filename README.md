# ApplyRadar weekly scan

Every Sunday and Wednesday at 9:00 AM Cairo, GitHub Actions checks the career sites of the companies in
`config.json` (Workday, Workable, Manatal and SuccessFactors sites, plus public LinkedIn listings for
Gulf pharma and cosmetics companies whose own sites can't be read) for jobs in Egypt and the GCC,
ranks them against two tracks (QC & Quality, Planning & Supply), and opens an issue with the new matches.
GitHub emails the issue to you. Cost: free (well inside the free Actions minutes).

- Change companies, keywords or locations: edit `config.json`.
- Run a scan now: Actions tab > Weekly job scan > Run workflow.
- Full results: `results/report.html`, `results/jobs.csv`, `results/jobs.json`.
