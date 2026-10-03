"""Receives Grafana alert webhooks at POST /alerts.

For each notification with new firing alerts it saves the alert and the telemetry around it
(metrics, logs, traces) in an incident folder, then starts the coding assistant on it.
Assistants run one at a time, because they all edit the same working tree.
"""
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI

from incident_response.assistant import run_assistant
from incident_response.context import Grafana, collect_context
from incident_response.incidents import IncidentStore, render_summary, write_json


SERVICE_DIR = Path(__file__).resolve().parent.parent
logger = logging.getLogger("incident_response")


@dataclass
class Settings:
    grafana_url: str = field(default_factory=lambda: os.getenv("GRAFANA_URL", "http://127.0.0.1:3000"))
    grafana_user: str = field(default_factory=lambda: os.getenv("GRAFANA_USER", ""))
    grafana_password: str = field(default_factory=lambda: os.getenv("GRAFANA_PASSWORD", ""))
    app_url: str = field(default_factory=lambda: os.getenv("APP_URL", "http://127.0.0.1:8000"))
    incidents_dir: Path = field(default_factory=lambda: Path(os.getenv("INCIDENTS_DIR", SERVICE_DIR / "incidents")))
    repo_dir: Path = field(default_factory=lambda: Path(os.getenv("REPO_DIR", SERVICE_DIR.parent)))
    assistant_command: str = field(default_factory=lambda: os.getenv("ASSISTANT_COMMAND", "claude"))
    assistant_enabled: bool = field(default_factory=lambda: os.getenv("ASSISTANT_ENABLED", "true").lower() != "false")


def create_app(settings=None, grafana=None, assistant=run_assistant):
    settings = settings or Settings()
    auth = (settings.grafana_user, settings.grafana_password) if settings.grafana_user else None
    grafana = grafana or Grafana(settings.grafana_url, auth=auth)
    store = IncidentStore(settings.incidents_dir)
    assistants = ThreadPoolExecutor(max_workers=1, thread_name_prefix="assistant")
    app = FastAPI(title="Incident Response")

    def start_assistant(incident):
        logger.info("Starting assistant for %s", incident.name)
        run = assistant(incident, settings.repo_dir, settings.assistant_command, settings.app_url)
        logger.info("Assistant finished for %s: %s", incident.name, run)

    def respond(incident, payload, alerts):
        """Runs after the response is sent, so Grafana's webhook does not time out."""
        contexts = [(alert, collect_context(grafana, alert)) for alert in alerts]
        write_json(incident / "context.json", [{"alert": alert, **context} for alert, context in contexts])
        (incident / "incident.md").write_text(render_summary(payload, contexts))
        logger.info("Saved incident context in %s", incident)
        if settings.assistant_enabled:
            assistants.submit(start_assistant, incident)

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.post("/alerts", status_code=202)
    def receive_alerts(payload: dict, background: BackgroundTasks):
        store.log_webhook(payload)
        firing = [alert for alert in payload.get("alerts", []) if alert.get("status") == "firing"]
        new = store.claim(firing)
        if not new:
            return {"status": "ignored", "reason": "no new firing alerts"}
        incident = store.create(payload, new)
        logger.info("Incident %s: %d new firing alert(s)", incident.name, len(new))
        background.add_task(respond, incident, payload, new)
        return {"status": "accepted", "incident": incident.name, "alerts": len(new)}

    app.state.assistants = assistants
    return app


def run():
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host=os.getenv("INCIDENT_HOST", "127.0.0.1"), port=int(os.getenv("INCIDENT_PORT", "8001")))
