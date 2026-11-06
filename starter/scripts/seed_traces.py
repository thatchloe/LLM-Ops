"""Run 10 diverse `/query` calls and emit rubric §7 evidence.

Sequentially POSTs a hand-curated question set against the live FAQ
service, waits for spans to flush, then prints two stdout artifacts
sized for direct paste into `WRITEUP.template.md` §7:

1. The same per-trace markdown table that `make show-traces` renders
   (via `src.tracing.trace_export.summarize_traces` +
   `render_markdown`).
2. A "slowest step across N traces" one-liner derived from per-span
   averages (e.g. `Slowest step across 10 traces: ChatCompletion
   (avg 1830ms, 52% of total request latency)`).

Usage:
    make serve         # in another terminal
    make seed-traces   # ~2 min wall clock; prints markdown table + slowest-step line
"""

import sys
import time

import httpx

from src.config import settings
from src.tracing.trace_export import STAGE_SPANS, render_markdown, summarize_traces

HEALTH_URL = "http://localhost:8080/health"
QUERY_URL = "http://localhost:8080/query"
SPAN_FLUSH_WAIT_S = 4.0  # let Phoenix span exporters drain after the last query

# Hand-curated 10-question pack — covers all four product categories
# (paddles, balls, accessories, apparel), mixes simple lookups with
# multi-product comparisons, includes two intentional repeats so the
# semantic cache fires on the second occurrence.
QUESTIONS: list[str] = [
    "What is the weight of the Selkirk AMPED S2?",                                    # paddles, simple, cache-priming
    "What is the weight of the Selkirk AMPED S2?",                                    # paddles, simple, cache HIT on #1
    "Compare the Selkirk Vanguard Power Air and JOOLA Hyperion CFS 16 paddles for tournament play.",  # paddles, complex, multi-product
    "What is the difference between indoor and outdoor balls?",                       # balls, complex, multi-product
    "Which ball is best for outdoor play in windy conditions?",                       # balls, simple
    "How much does the Franklin Sling Bag cost?",                                     # accessories, simple, cache-priming
    "How much does the Franklin Sling Bag cost?",                                     # accessories, simple, cache HIT on #6
    "How many paddles can the JOOLA Tour Elite Pro Duffel hold?",                     # accessories, simple
    "What material are the Engage Court Shorts made of?",                             # apparel, simple
    "Compare the moisture-wicking and durability properties of the apparel options you sell.",  # apparel, complex, multi-product
]


def _preflight() -> None:
    try:
        httpx.get(HEALTH_URL, timeout=5.0).raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(
            f"Could not reach {HEALTH_URL}. Start the server with `make serve` "
            f"in another terminal and try again. ({exc.__class__.__name__})",
            file=sys.stderr,
        )
        sys.exit(1)


def _post(question: str) -> dict:
    try:
        r = httpx.post(QUERY_URL, json={"question": question}, timeout=120.0)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        return {"error": f"{exc.__class__.__name__}: {exc}"}


def _fetch_spans():
    """Pull all spans from the in-process Phoenix server.

    Mirrors `scripts/show_traces.py::_fetch_spans` so we render against
    the same source of truth.

    Implementation note: stays on ``phoenix.Client`` (the in-process
    GraphQL accessor) rather than ``arize-phoenix-client``'s REST API.
    The REST endpoint of an embedded Phoenix UI doesn't surface
    in-memory spans the same way — switching emits a DeprecationWarning
    pointing to it, but the replacement returns an empty dataframe in
    embedded mode. Silence the warning locally; reconsider when the
    REST API gains parity.
    """
    import warnings

    import phoenix

    host = settings.phoenix_host
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"Migrate to .*arize-phoenix-client",
            category=DeprecationWarning,
        )
        client = phoenix.Client(endpoint=f"http://{host}:{settings.phoenix_port}")
        return client.get_spans_dataframe(
            project_name=settings.phoenix_project_name
        )


def _only_request_traces(df):
    """Restrict df to spans whose trace_id has a `query_request` root span.

    OpenAI calls made outside a request (such as inbox ingestion) and traces
    recorded before the root span existed are left out, so the rendered
    table only shows the full /query journey.
    """
    if df is None or len(df) == 0:
        return df
    request_ids = set(df.loc[df["name"] == "query_request", "context.trace_id"])
    return df[df["context.trace_id"].isin(request_ids)]


def _slowest_step_across_traces(df) -> str:
    """Return a `Slowest step across N traces: ...` one-liner.

    Computes per-child-span average duration across every captured
    trace, picks the highest, and reports the share of total root
    latency it explains. Falls back to a polite stub when traces
    haven't landed yet.
    """
    if df is None or len(df) == 0:
        return "Slowest step across 0 traces: (no spans captured — check Phoenix tracer)"

    children = df[df["name"].isin(STAGE_SPANS)].copy()
    roots = df[df["name"] == "query_request"]
    if len(children) == 0 or len(roots) == 0:
        return "Slowest step across 0 traces: (no query_request spans found)"

    children["_dur_ms"] = (
        (children["end_time"] - children["start_time"]).dt.total_seconds() * 1000
    )
    avg_by_name = children.groupby("name")["_dur_ms"].mean().sort_values(ascending=False)
    slowest_name = avg_by_name.index[0]
    slowest_avg_ms = float(avg_by_name.iloc[0])

    avg_root_ms = (
        ((roots["end_time"] - roots["start_time"]).dt.total_seconds() * 1000).mean()
    )
    pct = (slowest_avg_ms / avg_root_ms * 100) if avg_root_ms else 0.0
    n_traces = roots["context.trace_id"].nunique()

    return (
        f"Slowest step across {n_traces} traces: {slowest_name} "
        f"(avg {slowest_avg_ms:.0f}ms, {pct:.0f}% of total request latency)"
    )


def main() -> int:
    _preflight()

    print(f"Running {len(QUESTIONS)} traced queries against {QUERY_URL} ...", file=sys.stderr)
    for i, question in enumerate(QUESTIONS, 1):
        result = _post(question)
        marker = (
            "ERR " if "error" in result
            else "HIT " if result.get("cached") else "MISS"
        )
        print(f"  [{i:2d}/{len(QUESTIONS)}] {marker} {question[:70]}", file=sys.stderr)

    print(f"Waiting {SPAN_FLUSH_WAIT_S:.0f}s for span exporters to drain ...", file=sys.stderr)
    time.sleep(SPAN_FLUSH_WAIT_S)

    df = _fetch_spans()
    df_rag = _only_request_traces(df)
    summaries = summarize_traces(df_rag, last_n=len(QUESTIONS))
    total_traces = 0 if df_rag is None else len(df_rag.groupby("context.trace_id"))

    print(render_markdown(summaries, total_traces))
    print()
    print(_slowest_step_across_traces(df_rag))
    return 0


if __name__ == "__main__":
    sys.exit(main())
