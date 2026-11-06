"""Phoenix-backed RAG tracing.

Embedded mode (the default): ``phoenix.launch_app()`` starts an
in-process Phoenix UI at ``http://{host}:{port}`` — no Docker, no
signup. ``phoenix.otel.register(...)`` then registers an OpenTelemetry
``TracerProvider`` so spans are exported to that Phoenix instance, and
``OpenAIInstrumentor`` auto-traces every OpenAI SDK call.

``POST /query`` opens a ``query_request`` root span, so every stage of a
request shares one trace. ``trace_span`` creates the stage spans. The
decorator emits a ``rag_query`` span carrying RAG metadata (latency,
confidence, cost, top_k, model) around ``rag_retrieval`` and
``rag_generation``, which ``src.rag.pipeline`` opens around the real calls.
The trace_id (32-char hex) is injected back into the returned
``QueryResponse``.

When tracing is uninitialised (e.g. during tests, before ``init_tracing``
runs), OpenTelemetry returns a no-op tracer; the decorator still runs,
spans are dropped silently, and ``trace_id`` is ``None``.
"""

import functools
import os
import time
from contextlib import contextmanager
from typing import Callable, Iterator

from opentelemetry import trace as otel_trace
from opentelemetry.trace import Status, StatusCode

from src.config import settings
from src.models import QueryResponse
from src.rag import run_pipeline
from src.tracing.base import summarize_sources

_tracer_provider = None
_phoenix_session = None


def init_tracing() -> None:
    """Launch embedded Phoenix (if configured) and register the tracer.

    Idempotent — safe to call from multiple lifespan startups.
    """
    global _tracer_provider, _phoenix_session

    if settings.phoenix_embedded and _phoenix_session is None:
        import phoenix as px

        # Phoenix deprecated `launch_app(host=, port=)` kwargs in favour of
        # PHOENIX_HOST / PHOENIX_PORT env vars; setdefault preserves any
        # explicit override (e.g. PHOENIX_HOST_ROOT_PATH set by make
        # serve-proxy alongside these in the Udacity Workspace flow).
        os.environ.setdefault("PHOENIX_WORKING_DIR", settings.phoenix_working_dir)
        os.environ.setdefault("PHOENIX_HOST", settings.phoenix_host)
        os.environ.setdefault("PHOENIX_PORT", str(settings.phoenix_port))
        _phoenix_session = px.launch_app()

    if _tracer_provider is None:
        from openinference.instrumentation.openai import OpenAIInstrumentor
        from phoenix.otel import register

        endpoint = (
            f"http://{settings.phoenix_host}:{settings.phoenix_port}/v1/traces"
        )
        _tracer_provider = register(
            project_name=settings.phoenix_project_name,
            endpoint=endpoint,
            verbose=False,
        )
        OpenAIInstrumentor().instrument(tracer_provider=_tracer_provider)


def flush() -> None:
    """Force-flush queued spans on shutdown."""
    if _tracer_provider is not None:
        try:
            _tracer_provider.force_flush()
        except Exception:
            # Shutdown path — never let a flush failure crash the server
            pass


def _set_str_attr(span, key: str, value) -> None:
    """OTel attributes must be primitives; coerce non-primitives to str."""
    if isinstance(value, (str, int, float, bool)):
        span.set_attribute(key, value)
    else:
        span.set_attribute(key, str(value))


@contextmanager
def trace_span(name: str) -> Iterator[otel_trace.Span]:
    """Open a span in the current trace; a root span if there is none."""
    tracer = otel_trace.get_tracer("src.tracing.phoenix_backend")
    with tracer.start_as_current_span(name) as span:
        yield span


def trace_rag_query(
    fn: Callable[..., QueryResponse],
) -> Callable[..., QueryResponse]:
    """Wrap a function returning ``QueryResponse`` with a Phoenix root span."""

    @functools.wraps(fn)
    def wrapper(question: str, *args, **kwargs) -> QueryResponse:
        tracer = otel_trace.get_tracer("src.tracing.phoenix_backend")
        with tracer.start_as_current_span("rag_query") as span:
            span.set_attribute("input.value", question)
            for k, v in kwargs.items():
                _set_str_attr(span, f"rag.input.{k}", v)

            start = time.perf_counter()
            try:
                response = fn(question, *args, **kwargs)
            except Exception as exc:
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                span.record_exception(exc)
                raise

            latency_ms = (time.perf_counter() - start) * 1000

            span.set_attribute("output.value", response.answer)
            span.set_attribute("rag.latency_ms", latency_ms)
            span.set_attribute("rag.confidence", response.confidence)
            span.set_attribute("rag.cost_usd", response.cost_usd)
            span.set_attribute("rag.top_k", len(response.sources))
            span.set_attribute("rag.model", response.model)
            span.set_attribute(
                "rag.sources", str(summarize_sources(response))
            )

            ctx = span.get_span_context()
            trace_id = (
                format(ctx.trace_id, "032x") if ctx and ctx.is_valid else None
            )
            return response.model_copy(update={"trace_id": trace_id})

    return wrapper


@trace_rag_query
def traced_pipeline(
    question: str, model: str | None = None, top_k: int = 5
) -> QueryResponse:
    """``rag.run_pipeline`` with Phoenix instrumentation."""
    return run_pipeline(question, model=model, top_k=top_k)
