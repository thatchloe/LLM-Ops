"""No-op tracing backend.

Selected when ``settings.tracing_backend == "none"``. The decorator is
a pass-through; ``trace_id`` is always ``None``. Useful for environments
where no tracing infrastructure is reachable (CI, scratch dev) and for
tests that want to exercise the request flow without span machinery.
"""

import functools
from contextlib import contextmanager
from typing import Callable, Iterator

from opentelemetry import trace as otel_trace

from src.models import QueryResponse
from src.rag import run_pipeline


def init_tracing() -> None:
    return None


def flush() -> None:
    return None


@contextmanager
def trace_span(name: str) -> Iterator[otel_trace.Span]:
    yield otel_trace.INVALID_SPAN


def trace_rag_query(
    fn: Callable[..., QueryResponse],
) -> Callable[..., QueryResponse]:
    @functools.wraps(fn)
    def wrapper(question: str, *args, **kwargs) -> QueryResponse:
        response = fn(question, *args, **kwargs)
        return response.model_copy(update={"trace_id": None})

    return wrapper


@trace_rag_query
def traced_pipeline(
    question: str, model: str | None = None, top_k: int = 5
) -> QueryResponse:
    return run_pipeline(question, model=model, top_k=top_k)
