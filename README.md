# Production LLM FAQ Service — Capstone Starter

> **Are you a student starting this project?** Read [`INSTRUCTIONS.md`](INSTRUCTIONS.md)
> first. It walks through the graded deliverables, the workspace setup,
> and the submission requirements. The rubric is at
> [`rubric.md`](rubric.md).

You are building a production-ready FAQ service for **ThirdShotHub**, a pickleball e-commerce company. The service uses retrieval-augmented generation (RAG) to answer customer questions from product data, with the full LLM Ops stack: tracing, evaluation, guardrails, semantic caching, cost monitoring, file-based ingestion, and streaming.

This starter is the scaffold. Each layer is wired in but kept small and readable so you can extend it as the course progresses.

---

## Quick Start

Run these from the `starter/` directory.

```bash
# 1. Load/install dependencies (uv-managed virtualenv, pre-installed on Udacity workspace)
make setup

# 2. Copy and edit credentials
cp .env.example .env       # then fill in OPENAI_API_KEY (Phoenix tracing runs locally — no signup)

# 3. Load the seed product corpus into Chroma (writes to data/chroma/)
make load-data

# 4. Run the API
make serve-proxy            # Udacity Workspace: hosted at http://localhost:8080, /docs loads in the browser
make serve                  # Local environment: hosted at http://localhost:8080
```

Smoke-test it:

```bash
curl -X POST http://localhost:8080/query \
  -H 'Content-Type: application/json' \
  -d '{"question": "How heavy is the Selkirk AMPED S2?"}'
```

You should see a JSON `QueryResponse` with `answer`, retrieved `sources`, `confidence`, the model that handled it, token usage, and cost.

---

## Architecture at a Glance

```
                   ┌────────────────────────────────────────────────┐
   HTTP request    │                FastAPI app                     │
   ──────────────► │  src/gateway/app.py                            │
                   │    │                                           │
                   │    ├── POST /query    src/gateway/routes.py    │
                   │    ├── POST /query/stream                      │
                   │    │                  src/optimization/routes.py
                   │    └── GET  /cost-dashboard                    │
                   │                       src/cost/dashboard.py    │
                   └────────────────────────────────────────────────┘
                                          │
   POST /query flow (composed in routes.py):                        ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │ 1. detect_prompt_injection      src/guardrails/input_guards.py   │
   │       └── short-circuit on match → safe response (blocked_by)    │
   │ 2. detect_pii (redact)          src/guardrails/input_guards.py   │
   │ 3. cache_lookup                 src/cache/semantic.py            │
   │       └── on hit: return cached response                         │
   │ 4. classify (gpt-4o-mini)       src/gateway/classifier.py        │
   │ 5. select_model + traced_pipeline  src/gateway/router.py +       │
   │                                 src/tracing/phoenix_backend.py   │
   │ 6. retrieve (Chroma)            src/rag/retriever.py             │
   │ 7. generate (OpenAI chat)       src/rag/generator.py             │
   │ 8. log_request (cost)           src/cost/tracker.py              │
   │ 9. check_hallucination + is_off_topic                            │
   │                                 src/guardrails/output_guards.py  │
   │ 10. cache_store                 src/cache/semantic.py            │
   └──────────────────────────────────────────────────────────────────┘

   Sidecar process (independent of HTTP):
                            ┌────────────────────────────────────┐
                            │ data/inbox/*.json  →  watcher.py   │
                            │   src/ingestion/watcher.py         │
                            │   chunks → embeds → upserts to     │
                            │   Chroma; quarantines bad files    │
                            └────────────────────────────────────┘
```

External services: **OpenAI** (chat + embeddings). Everything else runs in-process: **Chroma** (vector DB and semantic cache, embedded `PersistentClient` writing to `data/chroma/`) and **Arize Phoenix** (tracing UI at http://localhost:6006). `TRACING_BACKEND=none` disables tracing for tests/CI. **No Docker required** at any layer. If port 6006 isn't reachable in your workspace, `make show-traces` exports the same trace data as markdown.

## Layout

```
project/
├── INSTRUCTIONS.md                   # graded deliverables + setup
├── rubric.md                         # grading criteria
├── README.md                         # this file (architecture + extension guide)
├── WRITEUP.template.md               # learner submission template
├── src/                              # application code (~1.8 KLOC)
│   ├── config.py                       # Pydantic settings, .env-loaded
│   ├── models.py                       # Source, TokenUsage, QueryResponse
│   ├── pricing.py                      # model → (input, output) USD/1M tokens
│   ├── gateway/                        # FastAPI app + HTTP routes + tiered routing
│   ├── rag/                            # retrieve → prompt → generate
│   ├── tracing/                        # in-process Phoenix; `TRACING_BACKEND=none` disables
│   ├── evaluation/                     # RAGAS over golden set
│   ├── cost/                           # JSONL log + HTML dashboard
│   ├── cache/                          # Chroma vector cache (separate `cache` collection)
│   ├── guardrails/                     # regex + LLM Guard (ML) implementations
│   ├── ingestion/                      # watchdog file-watcher
│   └── optimization/                   # streaming + TTFT measurement
├── prompts/                          # Jinja2 templates (rag_system.j2, classifier.j2)
├── data/                             # product corpus + inbox + golden_test_set.csv
├── tests/                            # 195 pytest tests; mirrors src/ tree
├── scripts/                          # operator entry points (load_data, run_eval, etc.)
├── Makefile                          # one-line entry points (setup, serve, eval)
└── pyproject.toml                    # uv-managed deps, Python 3.11
```

---

## Scripts

| Command | What it does |
|---|---|
| `make setup` | `uv sync` — install deps |
| `make serve` | `uvicorn src.gateway.app:app --reload --port 8080` |
| `make serve-proxy` | Same as `make serve` plus `--root-path /proxy/8080` and `PHOENIX_HOST_ROOT_PATH=/proxy/6006` (Udacity Workspace; lets `/docs` and the Phoenix UI load through the workspace proxy) |
| `make watch` | Run `scripts/start_watcher.py` against `data/inbox/` |
| `make load-data` | One-shot load of `data/products/*.json` into Chroma |
| `make eval` | Run RAGAS over `data/golden_test_set.csv` |
| `make install-guardrails-models` | Pre-download LLM Guard's ~400 MB of models |
| `make test` | `uv run pytest tests/ -q` |
| `make verify` | Run the project's verification checklist (`scripts/verify_capstone.py`) |

---

## How to Extend Each Layer

| If you want to… | Edit | Notes |
|---|---|---|
| Add a new product to the seed corpus | `data/products/*.json` then `make load-data` | Fields required: `product_id`, `name`, `category`, `brand`, `price`, `description`, `specifications` (object), `care_instructions`. |
| Auto-ingest products at runtime | Drop a JSON in `data/inbox/` while `make watch` is running | Malformed files move to `data/inbox/failed/<name>.error.txt`. Files over 256 KB are rejected. |
| Add a new LLM and price | `src/pricing.py::MODEL_PRICING` then refer to it from `.env` (`MODEL_COMPLEX` / `MODEL_SIMPLE`) | Pricing is USD per 1M tokens, `(input_price, output_price)`. |
| Tune the simple/complex routing prompt | `prompts/classifier.j2` | Returns JSON `{"classification": "simple"\|"complex"}`. Bad JSON falls through to `complex` (the safe-but-pricier default). |
| Block a new prompt-injection pattern | `src/guardrails/input_guards.py::INJECTION_PATTERNS` | Append a `re.compile(...)`. The handler short-circuits to `_safe_response` on first match. |
| Add a new PII type | `src/guardrails/input_guards.py::PII_PATTERNS` and `PII_REDACTIONS` | The redacted question flows to LLM, cache, and traces — not the raw value. |
| Use ML-backed guards instead of regex | Swap imports in `src/gateway/routes.py` from `src.guardrails.input_guards` to `src.guardrails.llm_guard.input_guards` | First scan downloads ~400 MB of HuggingFace models; cache them with `make install-guardrails-models`. |
| Tune cache similarity threshold | `src/cache/semantic.py::lookup` `threshold` arg, called from `routes.py` (default 0.95) | Lower threshold = more hits, more risk of returning a wrong-but-similar cached answer. |
| Adjust cache TTL | `src/cache/semantic.py::store` `ttl_s` arg (default 3600s) | Set to 0 to never expire. |
| Add a new hallucination check | `src/guardrails/output_guards.py::check_hallucination` | Output guards run after `route_query` and before `cache_store`. A flagged response never enters the cache. |
| Customize the system prompt | `prompts/rag_system.j2` | The `{{ contexts }}` placeholder is wrapped in `<<<BEGIN_CONTEXT>>>` markers — see the section below for why. |
| Add a new RAGAS metric | `src/evaluation/run_eval.py::DEFAULT_METRICS` | Pin RAGAS releases (`ragas==0.4.3`); 0.x patches break occasionally. |

---

## Prompt-Injection Hardening (`prompts/rag_system.j2`)

The retrieved context is wrapped between `<<<BEGIN_CONTEXT>>>` and `<<<END_CONTEXT>>>` markers, with an explicit instruction to the model that anything inside the markers is treated as data, never as commands. This protects against indirect prompt injection — a poisoned product description that tries to redirect the model. The pattern is the canonical mitigation for OWASP LLM01 (Prompt Injection) when retrieved content is mixed with system instructions in the same prompt.

If you remove the markers or relocate the instruction, run the security review test suite to confirm no regressions: `pytest tests/gateway/test_routes.py -v`.

## Glossary

A few terms used in source comments that aren't course terminology:

- **`route_query`** — the un-wrapped gateway call (no guards, no cache). Used directly by the cache wrapper and indirectly via the composed HTTP handler.
- **`guarded_route_query`** — the standalone wrapper from `src/guardrails/wrapper.py` that demonstrates the guard pattern in isolation. The composed HTTP handler in `src/gateway/routes.py` reproduces the same wiring inline so the request flow is visible at the boundary; the standalone wrapper remains for tests and module-by-module exploration.
- **`cached_route_query`** — same idea for the cache: standalone wrapper in `src/cache/wrapper.py`; the production path composes it inline.
- **`traced_pipeline`** — `run_pipeline` instrumented by Phoenix (or a no-op when `TRACING_BACKEND=none`). The HTTP handler calls this via `route_query`.
- **Forward-dependency rule** — packages in `src/` may only import from packages earlier in the curriculum order. Enforced by `tests/integration/test_dependency_graph.py`. Violations need an explicit, documented exception entry.

---

## Dependency Notes

A couple of pins in `pyproject.toml` are non-obvious and worth flagging:

- **`arize-phoenix>=13.21,<14`** + **`arize-phoenix-evals<3`** — Phoenix 14
  pulls in `authlib>=1.7` → `joserfc>=1.6` → `cryptography>=45.0.1`, which
  collides with `llm-guard==0.3.16` (current latest on PyPI) — its exact
  pin on `presidio-anonymizer==2.2.358` caps `cryptography<44.1`. There is
  no llm-guard release that fixes this yet. Phoenix 13 also needs the
  pre-restructure `phoenix.evals.models` layout, which only exists in
  `arize-phoenix-evals<3`. **Bump these together when llm-guard releases a
  version that allows newer presidio.**
- **`pytz>=2026.1`** — Phoenix imports `pytz` in `phoenix/datetime_utils.py`
  but doesn't declare it in its package metadata (an upstream packaging
  bug as of arize-phoenix 13.21 / 14.x). Drop this pin once Phoenix
  declares pytz themselves.
- **`openai>=1.30,<3`** — relaxed from the original `<2`. Our usage
  (`OpenAI(...)` constructor, `chat.completions.create`,
  `embeddings.create`, `stream_options`, `response_format`) is stable
  across both majors. The resolver currently lands on openai 1.109
  because llm-guard's `tiktoken` dep transitively pulls it that way.
