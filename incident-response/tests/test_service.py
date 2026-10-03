import base64
import json

import httpx
from fastapi.testclient import TestClient

from incident_response.assistant import build_command
from incident_response.context import Grafana
from incident_response.main import Settings, create_app


TRACE_ID = "0aa6eee19a4429001515cd21fb0d51e3"

ALERT = {
    "status": "firing",
    "labels": {
        "alertname": "Order Tracker 5xx responses",
        "service": "order-tracker",
        "http_request_method": "GET",
        "http_route": "/api/orders/{order_id}",
    },
    "annotations": {
        "summary": "GET /api/orders/{order_id} returned 5xx responses",
        "endpoint": "GET /api/orders/{order_id}",
        "time_window": "5m",
    },
    "startsAt": "2026-10-03T13:40:00Z",
    "fingerprint": "abc123",
}


def fake_grafana(request):
    path = request.url.path
    if path.endswith("/api/v1/query"):
        assert 'http_route="/api/orders/{order_id}"' in request.url.params["query"]
        return httpx.Response(200, json={"data": {"result": [
            {"metric": {"http_response_status_code": "500"}, "value": [0, "3"]},
            {"metric": {"http_response_status_code": "200"}, "value": [0, "7"]},
        ]}})
    if path.endswith("/loki/api/v1/query_range"):
        stream = {
            "severity_text": "ERROR", "exception_type": "ValueError",
            "exception_message": "day is out of range for month",
            "exception_stacktrace": "Traceback ...\nValueError: day is out of range for month",
            "code_file_path": "/app/app/main.py", "code_line_number": "120", "code_function_name": "get_order",
            "order_id": "express-1002", "trace_id": TRACE_ID,
        }
        return httpx.Response(200, json={"data": {"result": [{"stream": stream, "values": [
            ["1791034830853760768", "Order lookup failed"], ["1791034730853760768", "Order lookup failed"],
        ]}]}})
    if path.endswith("/api/search"):
        return httpx.Response(200, json={"traces": [{
            "traceID": TRACE_ID.lstrip("0"), "rootTraceName": "order.lookup",
            "startTimeUnixNano": "1791034830852010428", "durationMs": 7,
        }]})
    if path.endswith(f"/api/v2/traces/{TRACE_ID}"):
        span = {
            "traceId": base64.b64encode(bytes.fromhex(TRACE_ID)).decode(), "spanId": "VP/6T4hrwaA=",
            "name": "order.lookup", "status": {"code": "STATUS_CODE_ERROR"},
            "startTimeUnixNano": "1", "endTimeUnixNano": "7000001",
            "attributes": [{"key": "order.id", "value": {"stringValue": "express-1002"}}],
            "events": [{"name": "exception", "attributes": [
                {"key": "exception.type", "value": {"stringValue": "ValueError"}},
            ]}],
        }
        return httpx.Response(200, json={"trace": {"resourceSpans": [{"scopeSpans": [{"spans": [span]}]}]}})
    return httpx.Response(404)


def make_client(tmp_path, transport=httpx.MockTransport(fake_grafana)):
    runs = []

    def assistant(incident, repo_dir, command, app_url):
        runs.append(incident)
        return {"exit_code": 0}

    settings = Settings(incidents_dir=tmp_path / "incidents", repo_dir=tmp_path, app_url="http://app")
    app = create_app(settings, Grafana("http://grafana", transport=transport), assistant)
    return TestClient(app), app, runs


def wait_for_assistants(app):
    app.state.assistants.shutdown(wait=True)


def test_firing_alert_saves_context_and_starts_assistant(tmp_path):
    client, app, runs = make_client(tmp_path)

    response = client.post("/alerts", json={"status": "firing", "alerts": [ALERT]})
    wait_for_assistants(app)

    assert response.status_code == 202
    incident = tmp_path / "incidents" / response.json()["incident"]
    assert runs == [incident]
    summary = (incident / "incident.md").read_text()
    assert "`GET /api/orders/{order_id}`" in summary
    assert "| 500 | 3 |" in summary
    assert '"Order lookup failed" × 2' in summary
    assert "ValueError: day is out of range for month" in summary
    assert TRACE_ID in summary
    context = json.loads((incident / "context.json").read_text())
    assert context[0]["traces"]["traces"][0]["spans"][0]["trace_id"] == TRACE_ID
    assert json.loads((incident / "alert.json").read_text())["alerts"] == [ALERT]


def test_repeated_and_resolved_notifications_do_not_start_another_assistant(tmp_path):
    client, app, runs = make_client(tmp_path)

    client.post("/alerts", json={"alerts": [ALERT]})
    repeated = client.post("/alerts", json={"alerts": [ALERT]})
    resolved = client.post("/alerts", json={"alerts": [{**ALERT, "status": "resolved"}]})
    wait_for_assistants(app)

    assert repeated.json()["status"] == "ignored"
    assert resolved.json()["status"] == "ignored"
    assert len(runs) == 1
    assert len((tmp_path / "incidents" / "webhooks.jsonl").read_text().splitlines()) == 3


def test_handled_alerts_survive_restart(tmp_path):
    client, app, runs = make_client(tmp_path)
    client.post("/alerts", json={"alerts": [ALERT]})
    wait_for_assistants(app)

    client, app, runs = make_client(tmp_path)
    assert client.post("/alerts", json={"alerts": [ALERT]}).json()["status"] == "ignored"
    refired = client.post("/alerts", json={"alerts": [{**ALERT, "startsAt": "2026-10-03T15:00:00Z"}]})
    assert refired.json()["status"] == "accepted"


def test_unreachable_grafana_still_saves_alert_and_starts_assistant(tmp_path):
    client, app, runs = make_client(tmp_path, httpx.MockTransport(lambda request: httpx.Response(502)))

    response = client.post("/alerts", json={"alerts": [ALERT]})
    wait_for_assistants(app)

    incident = tmp_path / "incidents" / response.json()["incident"]
    assert "Could not query logs: HTTPStatusError" in (incident / "incident.md").read_text()
    assert len(runs) == 1


def test_assistant_runs_headless_with_incident_prompt(tmp_path):
    command = build_command("claude", tmp_path / "incidents" / "x", "http://app")

    assert command[:2] == ["claude", "-p"]
    assert f'{tmp_path / "incidents" / "x"}: start with incident.md' in command[2]
    assert "--allowedTools" in command
    assert "Do not commit" in command[2]
