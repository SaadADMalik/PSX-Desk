"""OpenTelemetry setup shared by every service.

One trace id follows a request across agents, the gateway, and (later) MCP and
A2A calls. httpx instrumentation injects the W3C `traceparent` header on every
outgoing call, and FastAPI instrumentation picks it up on the other side, so
nothing in our code has to pass trace ids around by hand.
"""

import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_configured = False


def setup_tracing(service_name: str, app=None) -> None:
    """Export to OTLP if OTEL_EXPORTER_OTLP_ENDPOINT is set; otherwise trace in-process only."""
    global _configured
    if not _configured:
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        HTTPXClientInstrumentor().instrument()
        _configured = True
    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)


def shutdown_tracing() -> None:
    """Flush pending spans; short-lived CLIs must call this before exiting."""
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        provider.shutdown()


def tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)
