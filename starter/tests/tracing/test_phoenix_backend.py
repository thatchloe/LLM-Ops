"""Tests for src.tracing.phoenix_backend."""

from unittest.mock import MagicMock, patch

import pytest
from opentelemetry.trace import StatusCode

from src.models import QueryResponse, Source, TokenUsage
from src.tracing.phoenix_backend import (
    flush,
    trace_rag_query,
    traced_pipeline,
)


def make_response(answer: str = "answer") -> QueryResponse:
    return QueryResponse(
        answer=answer,
        sources=[
            Source(doc_id="p1", chunk_text="A", similarity_score=0.9),
            Source(doc_id="p2", chunk_text="B", similarity_score=0.7),
        ],
        confidence=0.8,
        model="gpt-4o-mini",
        tokens=TokenUsage(prompt_tokens=100, completion_tokens=20),
        cost_usd=0.0042,
    )


def _build_mock_tracer(*, only_root: bool = False):
    """Build a mock tracer with separate root + generation span mocks.

    With ``only_root=True``, only one span context manager is queued —
    a second ``start_as_current_span`` call would raise StopIteration.
    Use that for the error path, which must short-circuit before the
    generation span.
    """
    trace_id_int = 0xABCDEF1234567890ABCDEF1234567890

    def make_span():
        span = MagicMock()
        ctx = MagicMock()
        ctx.trace_id = trace_id_int
        ctx.is_valid = True
        span.get_span_context.return_value = ctx

        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=span)
        cm.__exit__ = MagicMock(return_value=False)
        return span, cm

    root_span, root_cm = make_span()
    gen_span, gen_cm = make_span()

    tracer = MagicMock()
    if only_root:
        tracer.start_as_current_span.side_effect = [root_cm]
    else:
        tracer.start_as_current_span.side_effect = [root_cm, gen_cm]
    return tracer, root_span, gen_span


def _attrs(span) -> dict:
    """Collect set_attribute(key, value) calls into a dict."""
    return {c.args[0]: c.args[1] for c in span.set_attribute.call_args_list}


def test_decorator_creates_spans_and_returns_response_with_trace_id():
    tracer, root_span, _gen_span = _build_mock_tracer()

    with patch(
        "src.tracing.phoenix_backend.otel_trace.get_tracer",
        return_value=tracer,
    ):
        @trace_rag_query
        def fn(question, model=None):
            return make_response("Selkirk weighs 7.8 oz.")

        result = fn("How much does Selkirk weigh?", model="gpt-4o-mini")

    # The trace_id is the 32-char lowercase hex of the span's trace_id int
    assert result.trace_id == format(0xABCDEF1234567890ABCDEF1234567890, "032x")
    assert result.answer == "Selkirk weighs 7.8 oz."

    # Only the wrapper span: generation is timed inside run_pipeline
    span_names = [
        c.args[0] for c in tracer.start_as_current_span.call_args_list
    ]
    assert span_names == ["rag_query"]

    # Root span carries the RAG-level metadata
    root_attrs = _attrs(root_span)
    assert root_attrs["input.value"] == "How much does Selkirk weigh?"
    assert root_attrs["output.value"] == "Selkirk weighs 7.8 oz."
    assert root_attrs["rag.confidence"] == pytest.approx(0.8)
    assert root_attrs["rag.cost_usd"] == pytest.approx(0.0042)
    assert root_attrs["rag.top_k"] == 2
    assert root_attrs["rag.model"] == "gpt-4o-mini"
    assert root_attrs["rag.latency_ms"] >= 0
    # kwargs flowed through as rag.input.* attributes
    assert root_attrs["rag.input.model"] == "gpt-4o-mini"


def test_decorator_records_error_and_reraises():
    tracer, root_span, _gen_span = _build_mock_tracer(only_root=True)

    with patch(
        "src.tracing.phoenix_backend.otel_trace.get_tracer",
        return_value=tracer,
    ):
        @trace_rag_query
        def boom(question):
            raise RuntimeError("retrieval failed")

        with pytest.raises(RuntimeError, match="retrieval failed"):
            boom("any question")

    # The root span was marked ERROR and the exception recorded
    root_span.set_status.assert_called_once()
    status = root_span.set_status.call_args.args[0]
    assert status.status_code == StatusCode.ERROR
    root_span.record_exception.assert_called_once()

    # No second (generation) span was created — side_effect would raise
    assert tracer.start_as_current_span.call_count == 1


def test_traced_pipeline_calls_run_pipeline_through_decorator():
    tracer, _root_span, _gen_span = _build_mock_tracer()

    with patch(
        "src.tracing.phoenix_backend.otel_trace.get_tracer",
        return_value=tracer,
    ), patch(
        "src.tracing.phoenix_backend.run_pipeline",
        return_value=make_response("ans"),
    ) as run:
        result = traced_pipeline("Q", model="gpt-4o", top_k=3)

    run.assert_called_once_with("Q", model="gpt-4o", top_k=3)
    assert result.answer == "ans"
    assert result.trace_id is not None


def test_flush_calls_tracer_provider_force_flush():
    fake_provider = MagicMock()
    with patch(
        "src.tracing.phoenix_backend._tracer_provider", fake_provider
    ):
        flush()
    fake_provider.force_flush.assert_called_once()


def test_flush_is_silent_when_provider_uninitialised():
    """Before init_tracing runs, _tracer_provider is None — flush must no-op."""
    with patch("src.tracing.phoenix_backend._tracer_provider", None):
        flush()  # must not raise
