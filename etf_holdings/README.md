# ETF Holdings

Manual-dispatch workflow `ETF Holdings` (`.github/workflows/etf_holdings.yml`).
Scrapes the full Schwab holdings list (schwab.wallst.com, same source and ticker
rule as QQscan column G in `write_etf_leaders.py`) for every ETF in
`etf_list.txt` and uploads artifact `etf_holdings_out`:

- `holdings.csv` – row 1 = ETF ticker, rows 2+ = every holding ticker (no filtering)
- `status.csv` / `holdings.json` – per ETF: ok / empty / failed, counts vs Schwab's
  "of N matches", error text. Failed ETFs are retried once.

Differences from the G scraper (bug fixes, G itself is unchanged): sorts the table by
Symbol so tied weights don't reshuffle across pages, clicks explicit page numbers
(Next jumps a 5-page group), and checks row totals for completeness.
No secrets and no Sheet writes; the Sheet is written separately from the artifact.
