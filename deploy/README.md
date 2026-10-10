# Running the bot's metrics under systemd

`capture-bot-metrics.service` is a `oneshot` job: it runs the CLI (the same code
the desktop app uses), asks for the Prometheus text, and writes it to a file.
`capture-bot-metrics.timer` runs that job once a minute.

The file goes to the [node exporter textfile collector][textfile] folder, so the
metrics of a machine that has the bot but no web server still end up in
Prometheus.

```bash
sudo cp deploy/capture-bot-metrics.service deploy/capture-bot-metrics.timer /etc/systemd/system/
sudo systemctl edit capture-bot-metrics.service   # fix WorkingDirectory, --dir and --out
sudo systemctl daemon-reload
sudo systemctl enable --now capture-bot-metrics.timer
systemctl list-timers capture-bot-metrics.timer
cat /var/lib/node_exporter/textfile/capture_bot.prom
```

`--write-if-changed` means the file keeps its old timestamp while the numbers
stay identical: `capture_bot_last_success_timestamp_seconds` and
`capture_bot_health_state` then change exactly when the bot's health does.

## The rest of the bundle

| File | What it is |
| --- | --- |
| `rules.yml` | Prometheus alerting rules: metrics missing, never succeeded, no success for an hour, stale/empty history, queue growing or stuck, cap in sight, history growing. |
| `capture-bot.grafana.json` | A Grafana dashboard (import it, pick your Prometheus data source): seconds since the last success, days of room left, health state, queue, disk. |
| `capture-bot-healthcheck.service` + `.timer` | The dead-man's switch *at the systemd level*: runs `metrics --check --max-age 90` every 15 minutes and fails when the last successful capture is older than that. |
| `capture-bot-failed@.service` | A template to page a human from `OnFailure=`; edit its `ExecStart` for ntfy/Slack/PagerDuty. |

```bash
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl enable --now capture-bot-metrics.timer capture-bot-healthcheck.timer
systemctl list-timers 'capture-bot*'
systemctl --failed                       # the healthcheck shows up here when the bot is stuck
```

`--caps 500,50` on the timers tells the exposition what the configured caps are,
so `capture_bot_days_to_cap` is a real number (without it the gauge is -1 and the
"cap in sight" rule stays quiet). The same argument takes per-site budgets after
the two sizes - `--caps "500,50,news.example.com=200,*=1000"` (or `--site-caps`)
adds `capture_bot_site_cap_bytes{site="news.example.com"}` to the file, so the
host that will be trimmed first is visible in Grafana before it is trimmed. Run the check by hand to see what it says:

```bash
python -m app.cli metrics --dir /var/lib/capture-bot --check --max-age 90 --caps 500,50
# OK: The last successful capture was 4 minute(s) ago.
echo $?   # 1 when it is not (or when nothing ever succeeded)
python -m app.cli metrics --dir /var/lib/capture-bot --check --json   # same verdict, as data
```

Prometheus rules (the long version lives in `rules.yml`):

```yaml
- alert: CaptureBotStale
  expr: capture_bot_health_state{state="stale"} == 1
  for: 10m
- alert: CaptureBotNotSucceeding
  expr: time() - capture_bot_last_success_timestamp_seconds > 3600
  for: 5m
- alert: CaptureBotQueueGrowing
  expr: capture_bot_pending_alerts > 50
  for: 30m
- alert: CaptureBotCapInSight
  expr: capture_bot_days_to_cap >= 0 and capture_bot_days_to_cap < 7
  for: 30m
```

No systemd? A crontab line does the same:

```cron
* * * * * cd /opt/fullpage-capture-bot && python -m app.cli metrics --dir /var/lib/capture-bot --stale-after 30 --caps 500,50 --out /var/lib/node_exporter/textfile/capture_bot.prom --write-if-changed
*/15 * * * * python -m app.cli metrics --dir /var/lib/capture-bot --check --max-age 90 --caps 500,50 || /usr/local/bin/page-someone.sh
```

[textfile]: https://github.com/prometheus/node_exporter#textfile-collector
