import os
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("OTEL_SDK_DISABLED", "true")

from app import main  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    with TestClient(main.app) as test_client:
        yield test_client


def test_health_and_seeded_orders(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    orders = client.get("/api/orders").json()
    assert len(orders) == 3
    assert {order["priority"] for order in orders} == {"standard", "express"}


def test_create_and_update_order(client):
    response = client.post(
        "/api/orders",
        json={"customer": "Taylor", "item": "Mug", "priority": "standard"},
    )
    assert response.status_code == 201
    order_id = response.json()["id"]
    assert client.get(f"/api/orders/{order_id}").json()["status"] == "received"
    updated = client.patch(f"/api/orders/{order_id}", json={"status": "shipped"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "shipped"


def test_missing_order(client):
    assert client.get("/api/orders/missing").status_code == 404


def test_express_order_near_month_end_returns_estimated_delivery(client):
    placed_at = datetime(2026, 1, 31, tzinfo=timezone.utc)
    with sqlite3.connect(main.DB_PATH) as db:
        db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
            ("express-near-eom", "Jordan", "Cables", "express", "preparing", placed_at.isoformat()),
        )

    response = client.get("/api/orders/express-near-eom")

    assert response.status_code == 200
    assert response.json()["estimated_delivery"] == "2026-02-02"
