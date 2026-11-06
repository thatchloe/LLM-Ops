"""Tiered model routing — classify, dispatch to mini or 4o, log cost.

This is the integration hub: classify → choose model → run traced
pipeline → log per-request cost. Deliverable #3 of the project proposal
(measurable savings from tiered routing) is realised here.
"""

from src.config import settings
from src.cost.tracker import log_request
from src.gateway.classifier import QueryType, classify
from src.models import QueryResponse
from src.tracing import trace_span, traced_pipeline


def select_model(query_type: QueryType) -> str:
    """Map a query type to the configured model name."""
    return settings.model_simple if query_type == "simple" else settings.model_complex


def route_query(question: str, top_k: int = 5) -> QueryResponse:
    """Full request flow: classify, dispatch, trace, log."""
    with trace_span("classification") as span:
        query_type = classify(question)
        span.set_attribute("rag.query_type", query_type)
    model = select_model(query_type)
    response = traced_pipeline(question, model=model, top_k=top_k)
    log_request(model, response.tokens, response.cost_usd, query_type)
    return response
