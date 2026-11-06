"""HTTP API: POST /query, GET /health.

The ``/query`` handler composes the full LLM Ops stack the course teaches:
input guards (regex-based prompt-injection detection + PII redaction),
semantic cache lookup against the redacted question, the tiered model
router, and output guards (hallucination + off-topic). Cleaned questions
are cached only after passing the output guards. Blocked or filtered
requests return a safe ``QueryResponse`` with ``blocked_by`` set.

Layer-by-layer alternates (``guarded_route_query``, ``cached_route_query``,
the LLM-Guard variant) remain available as standalone wrappers for tests
and for module-by-module exploration in the curriculum.
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from src.cache.semantic import lookup as cache_lookup
from src.cache.semantic import store as cache_store
from src.gateway.router import route_query
from src.guardrails.llm_guard.input_guards import detect_pii, detect_prompt_injection
from src.guardrails.llm_judge.output_guards import check_hallucination, is_off_topic
from src.guardrails.wrapper import (
    SAFE_BLOCKED_MESSAGE,
    SAFE_FILTERED_MESSAGE,
    _safe_response,
)
from src.models import QueryResponse
from src.tracing import trace_span

router = APIRouter()


class QueryRequest(BaseModel):
    """Customer-facing FAQ request body.

    ``question`` is capped at 4000 characters as a cheap LLM04 (model DoS)
    mitigation: real FAQ questions never approach this size, and Pydantic
    rejects the request with HTTP 422 before any LLM or embedding call.
    ``top_k`` is bounded so a single request can't pull a runaway number
    of chunks out of Chroma.
    """

    question: str = Field(..., min_length=1, max_length=4000)
    top_k: int = Field(5, ge=1, le=20)


def _annotate_pii(response: QueryResponse, pii_found: list[str]) -> QueryResponse:
    """Tag a non-blocked response with which PII categories were redacted.

    The response itself is unchanged; only ``blocked_by`` becomes a
    descriptive marker (``pii_redacted: email,phone``) so callers can
    surface a notice without having to re-scan the answer.
    """
    if not pii_found:
        return response
    return response.model_copy(
        update={"blocked_by": f"pii_redacted: {','.join(pii_found)}"}
    )


@router.post("/query", response_model=QueryResponse)
async def query(request: QueryRequest) -> QueryResponse:
    """Run the full guarded + cached + traced FAQ pipeline.

    The handler intentionally inlines the layer composition rather than
    delegating to a single wrapper, because seeing the order of layers at
    the HTTP boundary is part of the lesson:

    1. ``detect_prompt_injection`` — short-circuits to a safe response on
       match. The LLM is never called for blocked input.
    2. ``detect_pii`` — redacts emails, phone numbers, SSNs, credit cards.
       The redacted text is what flows downstream; raw PII never reaches
       the cache, the LLM, or the tracing backend.
    3. ``cache_lookup`` — returns a cached ``QueryResponse`` if the
       embedding is within the similarity threshold of a prior query.
    4. ``route_query`` — classify simple/complex, dispatch to the right
       model, run the traced RAG pipeline, log per-request cost.
    5. ``check_hallucination`` and ``is_off_topic`` — output guards that
       block answers citing nonexistent products or veering off-topic.
       A flagged response is **not** cached.
    6. ``cache_store`` — persist the (cleaned-question, response) pair
       only after the output guards pass.

    Standalone wrappers (``guarded_route_query`` in ``src.guardrails`` and
    ``cached_route_query`` in ``src.cache``) compose the same pieces in
    isolation; they remain for tests and module-by-module exploration.

    The ``query_request`` span makes every layer part of one trace. Its
    input is set only after redaction, so raw PII never reaches tracing.
    """
    with trace_span("query_request") as root:
        with trace_span("input_guards"):
            if reason := detect_prompt_injection(request.question):
                return _safe_response(SAFE_BLOCKED_MESSAGE, blocked_by=reason)
            cleaned, pii_found = detect_pii(request.question)
        root.set_attribute("input.value", cleaned)

        with trace_span("cache_lookup"):
            cached = cache_lookup(cleaned)
        if cached:
            return _annotate_pii(cached, pii_found)

        response = route_query(cleaned, top_k=request.top_k)

        with trace_span("output_guards"):
            if reason := check_hallucination(response.answer, response.sources):
                return _safe_response(SAFE_FILTERED_MESSAGE, blocked_by=reason)
            if reason := is_off_topic(response.answer):
                return _safe_response(SAFE_FILTERED_MESSAGE, blocked_by=reason)

        with trace_span("cache_store"):
            cache_store(cleaned, response)
        return _annotate_pii(response, pii_found)


@router.get("/health")
async def health() -> dict:
    """Liveness probe. Returns 200 with no dependency checks.

    Intentionally cheap so a load balancer can hit it frequently. Does
    *not* verify Chroma or OpenAI connectivity — those are observed via
    the cost log and Phoenix traces, not at this seam.
    """
    return {"status": "ok"}
