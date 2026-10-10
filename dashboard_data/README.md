# Dashboard data

Drop the report files produced by your capture runs here (or point the
`Trend dashboard` workflow at another folder with the `history_dir` input):

```
capture-report-YYYYMMDD-HHMMSS.json   # written next to the screenshots
history.sqlite3                       # optional, faster index
```

The daily workflow builds `dashboard.html` from whatever it finds here and
publishes it to GitHub Pages, so the team can review change trends without
running the desktop app. Only the JSON reports are needed - screenshots stay on
the machine that captured them.
