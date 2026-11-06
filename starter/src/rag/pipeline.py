"""End-to-end RAG flow: retrieve -> prompt -> generate -> respond."""

from opentelemetry import trace as otel_trace

from src.config import settings
from src.models import QueryResponse
from src.rag.generator import generate
from src.rag.retriever import retrieve


def run_pipeline(
    question: str, model: str | None = None, top_k: int = 5
) -> QueryResponse:
    """Run the full RAG pipeline and return a structured QueryResponse."""
    chosen_model = model or settings.model_complex
    # Uses OpenTelemetry directly: src.tracing ranks after src.rag, and with
    # no provider registered these spans are no-ops.
    tracer = otel_trace.get_tracer("src.rag.pipeline")
    with tracer.start_as_current_span("rag_retrieval") as span:
        sources = retrieve(question, top_k=top_k)
        span.set_attribute("rag.retrieved_count", len(sources))
    with tracer.start_as_current_span("rag_generation") as span:
        answer, usage, cost = generate(question, sources, chosen_model)
        span.set_attribute("llm.model_name", chosen_model)
        span.set_attribute("input.value", question)
        span.set_attribute("output.value", answer)
        span.set_attribute("llm.token_count.prompt", usage.prompt_tokens)
        span.set_attribute("llm.token_count.completion", usage.completion_tokens)
        span.set_attribute("llm.token_count.total", usage.total)
        span.set_attribute("rag.cost_usd", cost)
    confidence = (
        sum(s.similarity_score for s in sources) / len(sources) if sources else 0.0
    )
    return QueryResponse(
        answer=answer,
        sources=sources,
        confidence=confidence,
        model=chosen_model,
        tokens=usage,
        cost_usd=cost,
    )
