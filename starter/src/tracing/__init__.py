"""Tracing backend selector.

The active backend is chosen at import time from ``settings.tracing_backend``:

- ``phoenix`` (default) — embedded Arize Phoenix at http://localhost:6006,
  no signup, no Docker.
- ``none`` — pass-through, no spans recorded. Useful for tests, CI,
  or any environment where the in-process Phoenix UI isn't wanted.

Each backend module exports the same five names; this module re-exports
the active backend's symbols so the rest of the codebase imports
``traced_pipeline``, ``trace_span``, ``flush``, ``init_tracing`` (and the
``trace_rag_query`` decorator for advanced use) without caring which
backend is on the other end.

For the rubric §7 evidence path when the Phoenix UI port isn't reachable
(e.g. some learner-workspace configurations), see
``scripts/show_traces.py`` — it reads the same in-process Phoenix store
via ``phoenix.Client`` and renders a markdown summary.
"""

from src.config import settings

if settings.tracing_backend == "none":
    from src.tracing.noop_backend import (
        flush,
        init_tracing,
        trace_rag_query,
        trace_span,
        traced_pipeline,
    )
else:  # "phoenix" (default) — covers any unrecognised value too
    from src.tracing.phoenix_backend import (
        flush,
        init_tracing,
        trace_rag_query,
        trace_span,
        traced_pipeline,
    )


__all__ = ["init_tracing", "flush", "trace_rag_query", "trace_span", "traced_pipeline"]
