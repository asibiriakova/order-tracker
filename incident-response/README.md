# Incident Response

Receives Grafana alerts at `POST /alerts` on port 8001. For each new firing alert it saves what's needed to understand the problem, then starts Claude Code in headless mode to investigate and fix it.

It runs on the host, not in Compose, because the assistant works in this repository's working tree with your Claude Code login. Grafana reaches it at `http://host.docker.internal:8001/alerts`. The contact point and notification policy are in [observability/grafana/provisioning/alerting/incident-response.yaml](../observability/grafana/provisioning/alerting/incident-response.yaml).

## Run it

You need `uv` and the `claude` CLI, signed in. Start the Compose stack first, then:

```bash
cd incident-response
APP_URL=http://127.0.0.1:18080 uv run incident-response
```

Run the tests with `uv run --frozen pytest -q`.

## What happens on an alert

1. The service logs every webhook to `incidents/webhooks.jsonl` and answers `202` at once.
2. It only acts on firing alerts it hasn't seen before. An alert counts as seen by its fingerprint plus start time, stored in `incidents/handled.json` so it survives restarts. Grafana's repeat and resolved notifications don't start another run.
3. It creates `incidents/<time>-<endpoint>/` and queries Prometheus, Loki and Tempo through Grafana's datasource proxy. The window runs from one alert window before the alert started until now.
   - `alert.json`: the Grafana payload, with labels, annotations, and dashboard and rule links.
   - `context.json`: the raw data. That is request counts by status for the endpoint, WARN and ERROR logs with stack traces and trace IDs, and failed traces with span details.
   - `incident.md`: a readable summary. Errors are grouped, with their count, code location, exception, stack trace and trace IDs.
4. It runs `claude -p` in the repository root. Claude is asked to find the root cause, fix it, add a regression test, run the tests, and write `report.md` in the incident folder. Claude runs with `acceptEdits` and a short tool allowlist: file tools, `uv run`, `curl`, and read-only git. It is told not to commit, push or redeploy. Runs are queued one at a time, because they share the working tree.
   - `assistant.jsonl`: the transcript (stream-json). Follow it live with `tail -f`.
   - `assistant.json`: the command, exit code, cost, and `session_id`. Continue a run with `claude --resume <session_id>`.

If one telemetry backend can't be queried, the error is recorded and the run continues with the rest.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `INCIDENT_HOST` / `INCIDENT_PORT` | `127.0.0.1` / `8001` | Listen address. On Linux, Docker's `host-gateway` doesn't reach `127.0.0.1`, so use `0.0.0.0` or the Docker bridge IP. |
| `GRAFANA_URL` | `http://127.0.0.1:3000` | Grafana, used to query the datasources |
| `GRAFANA_USER` / `GRAFANA_PASSWORD` | empty (anonymous) | Basic auth, if anonymous access is turned off |
| `APP_URL` | `http://127.0.0.1:8000` | Running app, so the assistant can reproduce the problem |
| `ASSISTANT_COMMAND` | `claude` | Assistant executable, plus any extra arguments |
| `ASSISTANT_ENABLED` | `true` | Set `false` to only collect context |
| `INCIDENTS_DIR` | `incident-response/incidents` | Where incidents are saved (git-ignored) |
| `REPO_DIR` | repository root | Where the assistant works |

The endpoint has no authentication: anything that can reach it can start the assistant. Keep it bound to localhost.
