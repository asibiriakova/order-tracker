"""Collect the telemetry around an alert: metrics, logs, and traces.

Loki and Tempo are only reachable inside the Compose network, so every query goes through
Grafana's datasource proxy. Grafana is published on the host and allows anonymous viewers.
"""
import base64
import json
import re
from datetime import datetime, timedelta, timezone

import httpx


MAX_TRACE_DETAILS = 5


class Grafana:
    def __init__(self, base_url, auth=None, transport=None):
        self.client = httpx.Client(base_url=base_url.rstrip("/"), auth=auth, timeout=15, transport=transport)

    def query(self, datasource_uid, path, params):
        response = self.client.get(f"/api/datasources/proxy/uid/{datasource_uid}{path}", params=params)
        response.raise_for_status()
        return response.json()


def parse_time(value, default):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return default
    # Grafana sends 0001-01-01 for "not set".
    return parsed if parsed.year > 1970 else default


def parse_duration(value, default=timedelta(minutes=5)):
    match = re.fullmatch(r"(\d+)([smh])", (value or "").strip())
    if not match:
        return default
    unit = {"s": "seconds", "m": "minutes", "h": "hours"}[match[2]]
    return timedelta(**{unit: int(match[1])})


def promql_string(value):
    return json.dumps(value)


def time_range(alert, now):
    """From one alert window before the alert started, to now."""
    window = parse_duration(alert.get("annotations", {}).get("time_window"))
    started = parse_time(alert.get("startsAt"), now)
    return started - window - timedelta(minutes=1), now


def collect_metrics(grafana, alert, start, end):
    labels = alert.get("labels", {})
    selector = ", ".join(
        [f'job={promql_string(labels.get("service", "order-tracker"))}']
        + [f"{name}={promql_string(labels[name])}" for name in ("http_request_method", "http_route") if name in labels]
    )
    seconds = max(int((end - start).total_seconds()), 60)
    queries = {
        "requests_by_status": f"sum by (http_response_status_code) (increase(http_server_requests_total{{{selector}}}[{seconds}s]))",
        "requests_by_status_total": f"sum by (http_response_status_code) (http_server_requests_total{{{selector}}})",
    }
    results = {}
    for name, expr in queries.items():
        data = grafana.query("prometheus", "/api/v1/query", {"query": expr, "time": end.timestamp()})
        results[name] = {
            "query": expr,
            "result": {
                item["metric"].get("http_response_status_code", "all"): float(item["value"][1])
                for item in data["data"]["result"]
            },
        }
    return results


def collect_logs(grafana, service, start, end, limit=200):
    """WARN and ERROR logs of the service, newest first."""
    query = f'{{service_name={promql_string(service)}}} | severity_number >= 13'
    data = grafana.query("loki", "/loki/api/v1/query_range", {
        "query": query,
        "start": int(start.timestamp() * 1e9),
        "end": int(end.timestamp() * 1e9),
        "limit": limit,
        "direction": "backward",
    })
    entries = []
    for stream in data["data"]["result"]:
        attributes = stream["stream"]
        for timestamp, line in stream["values"]:
            entries.append({
                "time": datetime.fromtimestamp(int(timestamp) / 1e9, timezone.utc).isoformat(),
                "message": line,
                "attributes": attributes,
            })
    entries.sort(key=lambda entry: entry["time"], reverse=True)
    return {"query": query, "entries": entries}


def group_logs(entries):
    """Group identical errors so the summary shows each problem once, with a count."""
    groups = {}
    for entry in entries:
        attributes = entry["attributes"]
        key = tuple(attributes.get(name, "") for name in (
            "severity_text", "exception_type", "exception_message", "code_file_path", "code_line_number",
        )) + (entry["message"],)
        group = groups.setdefault(key, {"count": 0, "first": entry["time"], "sample": entry, "trace_ids": []})
        group["count"] += 1
        group["first"] = entry["time"]
        if attributes.get("trace_id") and len(group["trace_ids"]) < 5:
            group["trace_ids"].append(attributes["trace_id"])
    return sorted(groups.values(), key=lambda group: -group["count"])


def otlp_id(value):
    """Tempo returns OTLP JSON with base64 IDs; show them as hex like everywhere else."""
    try:
        return base64.b64decode(value).hex()
    except (TypeError, ValueError):
        return value


def otlp_attributes(attributes):
    return {item["key"]: next(iter(item.get("value", {}).values()), None) for item in attributes or []}


def summarize_trace(trace):
    spans = []
    for resource_spans in trace.get("trace", trace).get("resourceSpans", []):
        for scope_spans in resource_spans.get("scopeSpans", []):
            for span in scope_spans.get("spans", []):
                spans.append({
                    "trace_id": otlp_id(span.get("traceId")),
                    "span_id": otlp_id(span.get("spanId")),
                    "name": span.get("name"),
                    "status": span.get("status", {}),
                    "duration_ms": (int(span.get("endTimeUnixNano", 0)) - int(span.get("startTimeUnixNano", 0))) / 1e6,
                    "attributes": otlp_attributes(span.get("attributes")),
                    "events": [
                        {"name": event.get("name"), "attributes": otlp_attributes(event.get("attributes"))}
                        for event in span.get("events", [])
                    ],
                })
    return spans


def collect_traces(grafana, service, start, end, limit=20):
    """Failed traces of the service, with full details for the most recent few."""
    query = f"{{resource.service.name={promql_string(service)} && status=error}}"
    data = grafana.query("tempo", "/api/search", {
        "q": query, "start": int(start.timestamp()), "end": int(end.timestamp()) + 1, "limit": limit,
    })
    found = sorted(data.get("traces", []), key=lambda trace: int(trace.get("startTimeUnixNano", 0)), reverse=True)
    traces = []
    for index, trace in enumerate(found):
        # Tempo search drops leading zeros from trace IDs.
        trace_id = trace["traceID"].rjust(32, "0")
        item = {
            "trace_id": trace_id,
            "root": trace.get("rootTraceName"),
            "start": datetime.fromtimestamp(int(trace.get("startTimeUnixNano", 0)) / 1e9, timezone.utc).isoformat(),
            "duration_ms": trace.get("durationMs"),
        }
        if index < MAX_TRACE_DETAILS:
            item["spans"] = summarize_trace(grafana.query("tempo", f"/api/v2/traces/{trace_id}", {}))
        traces.append(item)
    return {"query": query, "traces": traces}


def collect_context(grafana, alert, now=None):
    """Query each signal separately, so one failing backend does not lose the others."""
    now = now or datetime.now(timezone.utc)
    start, end = time_range(alert, now)
    service = alert.get("labels", {}).get("service", "order-tracker")
    context = {"time_range": {"start": start.isoformat(), "end": end.isoformat()}, "service": service}
    for name, collect in (
        ("metrics", lambda: collect_metrics(grafana, alert, start, end)),
        ("logs", lambda: collect_logs(grafana, service, start, end)),
        ("traces", lambda: collect_traces(grafana, service, start, end)),
    ):
        try:
            context[name] = collect()
        except Exception as error:  # noqa: BLE001 - keep whatever else we can get
            context[name] = {"error": f"{type(error).__name__}: {error}"}
    return context
