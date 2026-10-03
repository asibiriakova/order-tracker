# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup. You add telemetry, alerts, and an incident responder in Homework 4.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Stop the app with `docker compose down`. Add `-v` only if you also want to delete the order data.

## Telemetry

The app uses the OpenTelemetry SDK. In Docker Compose it sends all signals over OTLP to an OpenTelemetry Collector, which forwards metrics to Prometheus, logs to Loki, and traces to Tempo. Grafana reads all three.

- **Grafana**: <http://127.0.0.1:3000>. It opens the **Order Tracker – Requests and Errors** dashboard, which shows request counts, 4xx/5xx errors, error rate by route, warning and error logs, and failed traces. You can view without logging in. Sign in as `admin` / `admin` to edit. Log lines link to their trace, and traces link to their logs.
- **Alert**: **Order Tracker 5xx responses** (Alerting → Alert rules, folder *Order Tracker*) fires per endpoint (method and route) when it returned any 5xx in the last 5 minutes, and resolves after 5 minutes without one. Its annotations include the endpoint, the time window, and a link to the dashboard. Having no traffic, or no 5xx, counts as Normal. The rule is in [observability/grafana/provisioning/alerting/](observability/grafana/provisioning/alerting/). Grafana reads it at startup, so run `docker compose restart grafana` after you edit it.
- **Prometheus**: <http://127.0.0.1:9090>. Metrics are named `http_server_requests_total` and `http_server_request_duration_seconds`, with `job="order-tracker"`.
- Collector, Loki, and Tempo are only reachable inside the Compose network. Their configs are in [observability/](observability/).

Change the host ports with `GRAFANA_PORT` and `PROMETHEUS_PORT`, in the same way as `ORDER_TRACKER_PORT`. Telemetry data is kept in Docker volumes, and `docker compose down -v` deletes it along with the orders.

When `OTEL_EXPORTER_OTLP_ENDPOINT` is not set, such as when you run the app outside Compose, it prints all signals as JSON to stdout instead.

- **Metrics** (exported every 10 s, configurable with `OTEL_METRIC_EXPORT_INTERVAL` in ms): `http.server.requests` (counter) and `http.server.request.duration` (histogram, seconds). Both are labelled with `http.request.method`, `http.route` (the route template, such as `/api/orders/{order_id}`), and `http.response.status_code`.
- **Traces**: an `order.lookup` span for every order lookup, with `order.id`, `order.found`, `order.priority`, and `order.status`. Unexpected errors set the span status to `ERROR` and record the exception.
- **Logs**: `Order looked up` (INFO), `Order not found` (WARN), and `Order lookup failed` (ERROR, with the exception). Each log carries the trace and span IDs of its lookup.

Set `OTEL_SDK_DISABLED=true` to turn telemetry off. The tests set this.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.
