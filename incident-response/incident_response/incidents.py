"""Incident folders on disk: one per alert notification that brings new firing alerts.

    incidents/<id>/
      alert.json        the Grafana webhook payload
      context.json      metrics, logs, and traces around each new alert
      incident.md       readable summary; the assistant starts here
      assistant.jsonl   the assistant's transcript (stream-json)
      assistant.json    how the assistant was run, and its exit code
      report.md         written by the assistant
"""
import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from incident_response.context import group_logs


def alert_key(alert):
    """Grafana resends a firing alert on every group change and repeat; this identifies one occurrence."""
    return f'{alert.get("fingerprint", "")}@{alert.get("startsAt", "")}'


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "alert"


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")


class IncidentStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.handled_path = self.root / "handled.json"
        self.lock = threading.Lock()
        self.handled = set(json.loads(self.handled_path.read_text())) if self.handled_path.exists() else set()

    def log_webhook(self, payload):
        with self.lock, (self.root / "webhooks.jsonl").open("a") as log:
            log.write(json.dumps({"received_at": datetime.now(timezone.utc).isoformat(), "payload": payload}) + "\n")

    def claim(self, alerts):
        """Return the alerts not handled yet, and mark them handled."""
        with self.lock:
            new = [alert for alert in alerts if alert_key(alert) not in self.handled]
            self.handled.update(alert_key(alert) for alert in new)
            if new:
                write_json(self.handled_path, sorted(self.handled))
            return new

    def create(self, payload, alerts):
        first = alerts[0]
        name = first.get("annotations", {}).get("endpoint") or first.get("labels", {}).get("alertname", "")
        base = f'{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{slug(name)}'
        for attempt in range(1, 100):
            path = self.root / (base if attempt == 1 else f"{base}-{attempt}")
            try:
                path.mkdir()
                break
            except FileExistsError:
                continue
        write_json(path / "alert.json", {**payload, "alerts": alerts})
        return path


def fence(text):
    return f"```\n{text.rstrip()}\n```"


def render_summary(payload, contexts):
    """Markdown summary of the alerts and their evidence, for people and for the assistant."""
    lines = [f'# Incident: {payload.get("title") or payload.get("commonLabels", {}).get("alertname", "alert")}', ""]
    for alert, context in contexts:
        labels, annotations = alert.get("labels", {}), alert.get("annotations", {})
        lines += [
            f'## {annotations.get("summary") or labels.get("alertname", "Alert")}',
            "",
            f'- **Endpoint**: `{annotations.get("endpoint", "unknown")}`',
            f'- **Description**: {annotations.get("description", "")}',
            f'- **Started**: {alert.get("startsAt")}',
            f'- **Evidence window**: {context["time_range"]["start"]} to {context["time_range"]["end"]}',
            f'- **Labels**: `{json.dumps(labels)}`',
        ]
        for label, key in (("Alert rule", "generatorURL"), ("Dashboard", "dashboardURL"), ("Panel", "panelURL")):
            if alert.get(key):
                lines.append(f"- **{label}**: {alert[key]}")
        lines.append("")

        lines += ["### Requests by status code in the window", ""]
        metrics = context.get("metrics", {})
        if "error" in metrics:
            lines.append(f'Could not query metrics: {metrics["error"]}')
        else:
            counts = metrics["requests_by_status"]["result"]
            lines += ["| Status | Requests (approx.) |", "| --- | --- |"]
            lines += [f"| {status} | {round(count)} |" for status, count in sorted(counts.items())] or ["| none | 0 |"]
        lines.append("")

        lines += ["### Warning and error logs (grouped)", ""]
        logs = context.get("logs", {})
        if "error" in logs:
            lines.append(f'Could not query logs: {logs["error"]}')
        elif not logs["entries"]:
            lines.append("No WARN or ERROR logs in the window.")
        for group in group_logs(logs.get("entries", [])):
            attributes = group["sample"]["attributes"]
            location = ":".join(filter(None, (attributes.get("code_file_path"), attributes.get("code_line_number"))))
            lines += [
                f'#### {attributes.get("severity_text", "")} "{group["sample"]["message"]}" × {group["count"]}',
                "",
                f'- First: {group["first"]}, last: {group["sample"]["time"]}',
                f"- Logged at: `{location}` in `{attributes.get('code_function_name', '')}`",
            ]
            if attributes.get("exception_type"):
                lines.append(f'- Exception: `{attributes["exception_type"]}: {attributes.get("exception_message", "")}`')
            extra = {
                key: value for key, value in attributes.items()
                if not key.startswith(("code_", "exception_", "telemetry_", "service_", "severity_", "scope_"))
                and key not in {"detected_level", "flags", "observed_timestamp", "span_id", "trace_id"}
            }
            if extra:
                lines.append(f"- Attributes (latest): `{json.dumps(extra)}`")
            if group["trace_ids"]:
                lines.append(f'- Trace IDs: {", ".join(f"`{trace_id}`" for trace_id in group["trace_ids"])}')
            if attributes.get("exception_stacktrace"):
                lines += ["", fence(attributes["exception_stacktrace"])]
            lines.append("")

        lines += ["### Failed traces", ""]
        traces = context.get("traces", {})
        if "error" in traces:
            lines.append(f'Could not query traces: {traces["error"]}')
        elif not traces["traces"]:
            lines.append("No failed traces in the window.")
        for trace in traces.get("traces", []):
            lines.append(f'- `{trace["trace_id"]}` {trace["root"]} at {trace["start"]} ({trace["duration_ms"]} ms)')
            for span in trace.get("spans", []):
                lines.append(f'  - span `{span["name"]}` status `{json.dumps(span["status"])}` attributes `{json.dumps(span["attributes"])}`')
                for event in span["events"]:
                    event_attributes = {k: v for k, v in event["attributes"].items() if k != "exception.stacktrace"}
                    lines.append(f'    - event `{event["name"]}` `{json.dumps(event_attributes)}`')
        lines.append("")

    lines += [
        "## Notes",
        "",
        "- Logs and traces are for the whole service in the window: the app does not label them with the HTTP route.",
        "- Raw data is in `alert.json` and `context.json` next to this file.",
    ]
    return "\n".join(lines) + "\n"
