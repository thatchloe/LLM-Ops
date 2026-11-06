"""Input-side validators: prompt injection detection and PII redaction.

Lightweight regex/heuristic implementations chosen over Guardrails Hub
validators for capstone reliability. The Hub validators (e.g.
``hub://guardrails/detect_pii``) are the production upgrade path covered
in the Guardrails module's content; LLM Guard's ML-backed scanners (in
the sibling ``llm_guard/`` package) are a second upgrade path that runs
locally without external accounts.

To add a new injection pattern, append a ``re.compile(...)`` to
``INJECTION_PATTERNS``. To add a new PII type, add an entry to
``PII_PATTERNS`` and a matching redaction string in ``PII_REDACTIONS``.
"""

import re

INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bignore\s+(all\s+)?(previous|prior|above)\s+instructions?\b", re.IGNORECASE),
    re.compile(r"\bdisregard\s+(all\s+)?(previous|prior|above)\s+instructions?\b", re.IGNORECASE),
    re.compile(r"\byou\s+are\s+now\s+", re.IGNORECASE),
    re.compile(r"\bact\s+as\s+(an?\s+)?", re.IGNORECASE),
    re.compile(r"\bpretend\s+(to\s+be|you\s+are)\b", re.IGNORECASE),
    re.compile(r"^\s*system\s*[:>]", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*assistant\s*[:>]", re.IGNORECASE | re.MULTILINE),
    re.compile(r"<\s*\|?\s*im_start\s*\|?\s*>", re.IGNORECASE),
    re.compile(
        r"\b(reveal|show|print|leak|repeat|tell\s+me)\s+(your|the)\s+"
        r"(system|initial|original|hidden|secret)\s+(prompt|instructions?|rules?|message)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(bypass|override|turn\s+off|disable|ignore)\s+(your|the|all)?\s*"
        r"(safety|guardrails?|filters?|restrictions?|content\s+policy|policies)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(jailbreak\s+mode|DAN\s+mode|developer\s+mode|do\s+anything\s+now)\b",
        re.IGNORECASE,
    ),
    # Spoofed context delimiters: closing our own <<<END_CONTEXT>>> marker
    # (prompts/rag_system.j2) would let text pose as trusted instructions.
    re.compile(r"<<<\s*(BEGIN|END)_CONTEXT\s*>>>", re.IGNORECASE),
    # Chat-template control tokens from Llama-style prompt formats.
    re.compile(r"\[/?INST\]|<<\s*/?SYS\s*>>", re.IGNORECASE),
    # "Forget" phrasing of an instruction reset. The possessive/ordinal is
    # required so "did I forget the care instructions?" stays legitimate.
    re.compile(
        r"\b(forget|erase|wipe)\s+(all\s+|everything\s+|any\s+)?(of\s+)?"
        r"(your|previous|prior|above|earlier)\s+(previous\s+|prior\s+)?"
        r"(instructions?|rules?|prompts?|guidelines)\b",
        re.IGNORECASE,
    ),
    # Encoded-payload smuggling: decode a blob, then act on its contents.
    re.compile(
        r"\b(decode|decrypt|convert)\b.{0,40}\b(base64|rot13|hex)\b"
        r".{0,60}\b(follow|execute|run|obey|do\s+what)\b",
        re.IGNORECASE,
    ),
]

PII_PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "phone": re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]*?){13,16}\b"),
}

PII_REDACTIONS: dict[str, str] = {
    "email": "[REDACTED_EMAIL]",
    "phone": "[REDACTED_PHONE]",
    "ssn": "[REDACTED_SSN]",
    "credit_card": "[REDACTED_CARD]",
}


def detect_prompt_injection(text: str) -> str | None:
    """Return a reason string if the input looks like prompt injection, else None."""
    for pattern in INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            return f"prompt_injection: matched pattern {pattern.pattern!r}"
    return None


def detect_pii(text: str) -> tuple[str, list[str]]:
    """Redact known PII kinds from ``text``. Returns ``(redacted, kinds_found)``."""
    found: list[str] = []
    redacted = text
    for kind, pattern in PII_PATTERNS.items():
        if pattern.search(redacted):
            found.append(kind)
            redacted = pattern.sub(PII_REDACTIONS[kind], redacted)
    return redacted, found
