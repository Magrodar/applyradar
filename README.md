# ApplyRadar weekly scan

Every Tuesday at 9:00 AM Cairo, GitHub Actions checks the public Workday career sites of
GSK, Novartis, Pfizer, Viatris, AstraZeneca, Sanofi, P&G and Unilever for jobs in Egypt and the Gulf,
ranks them against two tracks (QC & Quality, Planning & Supply), and opens an issue with the new matches.
GitHub emails the issue to you. Cost: free (well inside the free Actions minutes).

- Change companies, keywords or locations: edit `config.json`.
- Run a scan now: Actions tab > Weekly job scan > Run workflow.
- Full results: `results/report.html`, `results/jobs.csv`, `results/jobs.json`.
