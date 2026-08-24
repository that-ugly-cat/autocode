"""
Launching a coding run — one path, whichever surface asked.

This module exists because a run *spends money*. The web form and the MCP tool
both take the same six knobs, and if the two validated them separately they
would drift: one would forget that Excel corpora have no sequential context,
or that the dictionary engine matches nothing without expressions, and the way
that failure surfaces is a bill. So the rules live here and the surfaces are
thin — a route that turns a ValueError into a 400, a tool that turns it into a
message the model can read.

`preflight` is the part worth naming. Three things stop a run before it starts:
no Anthropic key on the caller's profile, no study context on the workspace, no
expressions in the codebook when the engine is the dictionary. All three are
cheap to check and expensive to discover halfway through a corpus.
"""
import json
import threading

import coding
import conventions
from models import Code, CodeExpression, Document, Run, RunDocument, PRICING

DOCX_SEG_MODES = {"document", "utterance_regex", "paragraph", "sentence"}
EXCEL_SEG_MODES = {"cell", "sentence"}
ENGINES = ("llm", "dictionary")


class RunSpec:
    """What both surfaces pass in. Plain object, so the MCP tools do not have to
    build a pydantic model to ask a question about cost."""

    def __init__(self, document_ids, unit=None, engine="llm", model="claude-sonnet-4-6",
                 context_window=3, max_workers=5, excluded_roles=None):
        self.document_ids = list(document_ids or [])
        self.unit = unit or None
        self.engine = engine
        self.model = model
        self.context_window = context_window
        self.max_workers = max_workers
        self.excluded_roles = list(excluded_roles or [])


def active_codes(ws, db) -> list[Code]:
    return db.query(Code).filter(Code.workspace_id == ws.id,
                                 Code.is_deleted == False).all()  # noqa: E712


def has_expressions(ws, db) -> bool:
    """Whether the active codebook carries any dictionary expression at all."""
    return bool(db.query(CodeExpression.id)
                .join(Code, Code.id == CodeExpression.code_id)
                .filter(Code.workspace_id == ws.id,
                        Code.is_deleted == False).first())  # noqa: E712


def allowed_units(ws) -> set:
    return EXCEL_SEG_MODES if ws.input_type == "excel" else DOCX_SEG_MODES


def resolve_unit(ws, unit: str | None) -> str:
    """The coding unit for this run — the caller's, or the workspace's legacy default."""
    unit = unit or ws.segmentation_mode
    allowed = allowed_units(ws)
    if unit not in allowed:
        raise ValueError("Invalid coding unit for this corpus "
                         f"(allowed: {', '.join(sorted(allowed))})")
    return unit


def validate(spec: RunSpec, ws, db) -> tuple[list[Document], str, int]:
    """Documents, coding unit and effective context window, or ValueError."""
    if spec.engine not in ENGINES:
        raise ValueError(f"Invalid engine (one of: {', '.join(ENGINES)})")
    bad = [r for r in spec.excluded_roles if r not in conventions.ROLES]
    if bad:
        raise ValueError(f"Unknown roles: {', '.join(bad)}")
    if spec.model not in PRICING:
        raise ValueError(f"Unknown model (one of: {', '.join(sorted(PRICING))})")
    if not (0 <= spec.context_window <= 20):
        raise ValueError("Context window must be 0–20")
    if not (1 <= spec.max_workers <= 10):
        raise ValueError("Max workers must be 1–10")
    unit = resolve_unit(ws, spec.unit)
    docs = (db.query(Document)
            .filter(Document.workspace_id == ws.id,
                    Document.id.in_(spec.document_ids)).all())
    if not docs:
        raise ValueError("Select at least one document")
    # No sequential context for respondents, whole documents or keyword matching:
    # there is no "previous utterance" to disambiguate against.
    ctx = 0 if (ws.input_type == "excel" or unit == "document"
                or spec.engine == "dictionary") else spec.context_window
    return docs, unit, ctx


def preflight(spec_engine: str, ws, user, db) -> None:
    """What must be true before a run is worth starting. Raises ValueError."""
    if spec_engine == "llm":
        if not user.api_key_encrypted:
            raise ValueError("Save your Anthropic API key in the profile first")
        if not (ws.study_context or "").strip():
            raise ValueError("Set the study context in the workspace settings "
                             "before starting a run")
        # An empty codebook is fine for the LLM — that is inductive coding, and
        # the model proposes the codes. The dictionary engine is the opposite
        # case: with no expressions it can match nothing at all.
    elif spec_engine == "dictionary" and not has_expressions(ws, db):
        raise ValueError("The dictionary engine needs codes with expressions — your "
                         "codebook has none. Add expressions in the codebook, or use "
                         "the LLM engine.")


def estimate(spec: RunSpec, ws, db) -> dict:
    """Segments, tokens, cost and ETA — before anything is spent."""
    docs, unit, ctx = validate(spec, ws, db)
    est = coding.estimate_run_cost(ws, docs, unit, ctx, spec.model,
                                   active_codes(ws, db),
                                   excluded_roles=spec.excluded_roles)
    if spec.engine == "dictionary":  # free and local: only the segment count informs
        est.update({"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
    est["eta_seconds"] = coding.estimate_run_seconds(spec.engine, est["segments"],
                                                     spec.max_workers)
    est["unit"] = unit
    est["documents"] = len(docs)
    return est


def launch(spec: RunSpec, ws, user, db) -> Run:
    """Create the run, snapshot the settings, and start coding in a thread."""
    preflight(spec.engine, ws, user, db)
    docs, unit, ctx = validate(spec, ws, db)
    run = Run(workspace_id=ws.id, created_by_id=user.id, status="pending",
              granularity=unit, engine=spec.engine, model=spec.model,
              context_window=ctx, max_workers=spec.max_workers,
              excluded_roles_snapshot=json.dumps(sorted(set(spec.excluded_roles))))
    db.add(run)
    db.flush()
    for doc in docs:
        db.add(RunDocument(run_id=run.id, document_id=doc.id, status="pending"))
    db.commit()
    threading.Thread(target=coding.execute_run, args=(run.id,), daemon=True).start()
    return run
