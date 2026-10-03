import logging
import os
import sys
import time

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter


SERVICE_NAME = "order-tracker"
METRIC_EXPORT_INTERVAL_MS = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "10000"))

tracer = trace.get_tracer(SERVICE_NAME)
meter = metrics.get_meter(SERVICE_NAME)
logger = logging.getLogger(SERVICE_NAME)

request_counter = meter.create_counter(
    "http.server.requests",
    unit="{request}",
    description="HTTP requests handled, by route and status code",
)
request_duration = meter.create_histogram(
    "http.server.request.duration",
    unit="s",
    description="HTTP request duration, by route and status code",
    explicit_bucket_boundaries_advisory=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
)


def setup_telemetry():
    """Install SDK providers for traces, metrics, and logs.

    Signals go over OTLP/HTTP when OTEL_EXPORTER_OTLP_ENDPOINT is set (the exporters read
    it themselves), and are printed to stdout otherwise.
    """
    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        return
    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return
    resource = Resource.create({"service.name": SERVICE_NAME})
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        span_exporter, metric_exporter, log_exporter = (
            OTLPSpanExporter(), OTLPMetricExporter(), OTLPLogExporter()
        )
    else:
        span_exporter, metric_exporter, log_exporter = (
            ConsoleSpanExporter(out=sys.stdout),
            ConsoleMetricExporter(out=sys.stdout),
            ConsoleLogExporter(out=sys.stdout),
        )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=METRIC_EXPORT_INTERVAL_MS)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    set_logger_provider(logger_provider)
    logger.addHandler(LoggingHandler(logger_provider=logger_provider))
    logger.setLevel(logging.INFO)
    logger.propagate = False


async def record_request(request, call_next):
    """HTTP middleware: count requests and time them, labelled by route template and status."""
    start = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        attributes = {
            "http.request.method": request.method,
            "http.route": getattr(route, "path", "unmatched"),
            "http.response.status_code": status_code,
        }
        request_counter.add(1, attributes)
        request_duration.record(time.perf_counter() - start, attributes)

