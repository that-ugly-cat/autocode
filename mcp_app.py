"""
The model-facing surface of AutoCode.

A codebook is written in conversation and then typed into a form — twice, once
in the chat where it was reasoned about and once in the browser where it has to
live. This surface removes the second half: draft the codes where the thinking
happens, push them in one call, launch the run, read back what the model coded
and with what rationale.

Access. Every call runs as the human who owns the API key, and every workspace
lookup goes through auth.mcp_workspace(), which is the same workspace_for() the
web app uses. The MCP surface therefore has exactly the reach of its owner, no
more. A workspace the caller is not a member of reports "not found" rather than
"forbidden", so the model cannot enumerate what it cannot see.

Spending. `start_run` is the only tool that costs money, it spends the caller's
own Anthropic key and nobody else's, and `estimate_run` sits next to it for
free. Ask for the estimate, show it, then start — a corpus of interviews is not
a place to find out afterwards.

Errors are returned as {"error": ...} rather than raised: a tool that throws
gives the model a stack trace to hallucinate around, while a message it can
read lets it correct course.
"""
import json
from datetime import datetime

from mcp.server.mcpserver import MCPServer

import analysis as analysis_mod
import auth
import conventions
import runs as runs_mod
from models import (PRICING, Code, CodeExpression, Coding, Document, Run,
                    RunSegment, SessionLocal, User, Workspace, WorkspaceMember,
                    normalize_label, owns_workspace, workspace_for)
from segmentation import SPACY_MODELS

mcp = MCPServer(
    name="autocode",
    instructions=(
        "LLM-assisted and dictionary-based qualitative coding. Call "
        "list_workspaces first — every other tool takes a workspace id or name. "
        "Reads are free; before any write, and above all before start_run, "
        "confirm with the user: a run spends their Anthropic credit. "
        "estimate_run costs nothing and answers how much."
    ),
)


def _fail(msg: str) -> dict:
    return {"error": msg}


def _d(dt) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M") if dt else None


# ── Shapes ────────────────────────────────────────────────────────────────────

def _ws_brief(ws: Workspace, db, user: User) -> dict:
    n_docs = db.query(Document).filter(Document.workspace_id == ws.id).count()
    n_codes = len(runs_mod.active_codes(ws, db))
    n_runs = db.query(Run).filter(Run.workspace_id == ws.id).count()
    return {
        "id": ws.id, "name": ws.name, "description": ws.description,
        "input_type": ws.input_type,
        "role": "owner" if owns_workspace(user, ws) else "member",
        "documents": n_docs, "codes": n_codes, "runs": n_runs,
        "url": f"/workspace/{ws.id}",
    }


def _doc_row(doc: Document) -> dict:
    return {
        "id": doc.id, "name": doc.display_name, "source": doc.source_type,
        "language": doc.language, "group": doc.group_label,
        "convention": doc.convention,
        "roles": json.loads(doc.roles_json) if doc.roles_json else None,
        "uploaded_at": _d(doc.uploaded_at),
    }


def _code_row(code: Code, db, with_expressions: bool = False) -> dict:
    row = {
        "id": code.id, "label": code.label, "cluster": code.cluster,
        "description": code.description, "example": code.example,
        "model_proposed": bool(code.is_model_proposed),
        "proposed_in_run": code.proposed_in_run_id,
    }
    if with_expressions:
        exprs: dict = {}
        for e in (db.query(CodeExpression)
                  .filter(CodeExpression.code_id == code.id).all()):
            exprs.setdefault(e.language, []).append(e.expression)
        row["expressions"] = exprs
    return row


def _run_row(run: Run, db) -> dict:
    n_codings = db.query(Coding).filter(Coding.run_id == run.id).count()
    return {
        "id": run.id, "status": run.status, "engine": run.engine,
        "unit": run.granularity, "model": run.model if run.engine == "llm" else None,
        "documents": len(run.run_documents), "codings": n_codings,
        "cost_usd": round(run.cost_usd or 0.0, 4),
        "started_at": _d(run.started_at), "completed_at": _d(run.completed_at),
        "error": run.error_message,
        "url": f"/workspace/{run.workspace_id}/runs/{run.id}",
    }


def _run_for(db, run_id: int) -> Run:
    """A run the caller may see, resolved through the same access rule as the web."""
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise LookupError(f"No run {run_id}")
    auth.mcp_workspace(db, str(run.workspace_id))
    return run


def _code_for(db, code_id: int) -> Code:
    code = (db.query(Code)
            .filter(Code.id == code_id, Code.is_deleted == False).first())  # noqa: E712
    if not code:
        raise LookupError(f"No code {code_id}")
    auth.mcp_workspace(db, str(code.workspace_id))
    return code


# ══════════════════════════════════════════════════════════════════════════════
# READS
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def list_workspaces() -> dict:
    """Workspaces the caller can reach, with the size of each corpus and codebook."""
    db = SessionLocal()
    try:
        user = auth.current_caller()
        rows = db.query(Workspace).order_by(Workspace.created_at.desc()).all()
        mine = [w for w in rows if workspace_for(db, user, w.id) is not None]
        return {"you": user.name,
                "workspaces": [_ws_brief(w, db, user) for w in mine]}
    except PermissionError as e:
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def get_workspace(workspace: str) -> dict:
    """
    One workspace in full: settings, team, corpus and codebook digests, recent runs.

    `ready` is the part to read before proposing a run: it lists what would stop
    one — no Anthropic key on your profile, no study context, no expressions for
    the dictionary engine. The study context goes into the coding system prompt,
    so a workspace without one codes without knowing what the study is about.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_workspace(db, workspace)
        user = auth.current_caller()
        docs = db.query(Document).filter(Document.workspace_id == ws.id).all()
        codes = runs_mod.active_codes(ws, db)
        by = lambda key: {k: sum(1 for d in docs if key(d) == k)  # noqa: E731
                          for k in sorted({key(d) or "" for d in docs})}
        recent = (db.query(Run).filter(Run.workspace_id == ws.id)
                  .order_by(Run.id.desc()).limit(5).all())
        blockers = []
        if not user.api_key_encrypted:
            blockers.append("no Anthropic API key on your profile (LLM engine)")
        if not (ws.study_context or "").strip():
            blockers.append("no study context set (LLM engine)")
        if not runs_mod.has_expressions(ws, db):
            blockers.append("no code carries expressions (dictionary engine)")
        out = _ws_brief(ws, db, user)
        out.update({
            "study_context": ws.study_context,
            "segmentation": {
                "default_unit": ws.segmentation_mode,
                "allowed_units": sorted(runs_mod.allowed_units(ws)),
                "regex": ws.segmentation_regex,
                "language": ws.segmentation_language,
            },
            "excluded_roles_default": json.loads(ws.excluded_roles_json)
            if ws.excluded_roles_json else [],
            "members": [m.user.name for m in ws.members],
            "owner": ws.owner.name,
            "corpus": {"documents": len(docs), "by_language": by(lambda d: d.language),
                       "by_group": by(lambda d: d.group_label),
                       "by_convention": by(lambda d: d.convention)},
            "codebook": {"codes": len(codes),
                         "clusters": sorted({(c.cluster or "").strip()
                                             for c in codes if (c.cluster or "").strip()}),
                         "with_expressions": sum(
                             1 for c in codes
                             if db.query(CodeExpression.id)
                                  .filter(CodeExpression.code_id == c.id).first())},
            "recent_runs": [_run_row(r, db) for r in recent],
            "ready": {"ok": not blockers, "blockers": blockers},
        })
        return out
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def list_documents(workspace: str, group: str = "", language: str = "",
                   limit: int = 200) -> dict:
    """
    The corpus: one row per document, with the metadata that decides how it is cut.

    `convention` is the transcript convention used to split it into utterances;
    a document with none is coded whole. `roles` maps speaker labels to
    interviewer / participant / other, which is what run-time role exclusion
    acts on. Uploading is not here: files are binary and belong to the web app.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_workspace(db, workspace)
        q = db.query(Document).filter(Document.workspace_id == ws.id)
        docs = q.order_by(Document.id).all()
        if group:
            docs = [d for d in docs if (d.group_label or "") == group]
        if language:
            docs = [d for d in docs if (d.language or "") == language]
        return {"workspace": ws.name, "count": len(docs),
                "documents": [_doc_row(d) for d in docs[:limit]]}
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def get_codebook(workspace: str, cluster: str = "",
                 with_expressions: bool = False) -> dict:
    """
    The active codebook, grouped by cluster.

    A code carries a label and a natural-language description, which is what the
    LLM engine interprets, and optionally per-language expressions, which is what
    the dictionary engine matches. The two can live on the same code; set
    `with_expressions` to see them.

    Deleted codes are not here: deletion is soft, so past runs stay readable, but
    the codebook a run would use is this one.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_workspace(db, workspace)
        codes = runs_mod.active_codes(ws, db)
        if cluster:
            needle = cluster.strip().lower()
            codes = [c for c in codes if (c.cluster or "").strip().lower() == needle]
        codes.sort(key=lambda c: ((c.cluster or "~").lower(), c.label.lower()))
        return {"workspace": ws.name, "count": len(codes),
                "codes": [_code_row(c, db, with_expressions) for c in codes]}
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def list_runs(workspace: str, limit: int = 20) -> dict:
    """Coding runs in a workspace, newest first."""
    db = SessionLocal()
    try:
        ws = auth.mcp_workspace(db, workspace)
        rows = (db.query(Run).filter(Run.workspace_id == ws.id)
                .order_by(Run.id.desc()).limit(limit).all())
        return {"workspace": ws.name, "count": len(rows),
                "runs": [_run_row(r, db) for r in rows]}
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def get_run(run_id: int) -> dict:
    """
    One run: progress, cost, per-document status, and the code tally once it lands.

    `uncoded` counts units the engine saw and left alone — the negative half of
    the photograph, and the one worth reading before trusting the positive half.
    `excluded` are units that were never codable (front matter, excluded roles),
    so they inflate no denominator.
    """
    db = SessionLocal()
    try:
        run = _run_for(db, run_id)
        out = _run_row(run, db)
        segs = db.query(RunSegment).filter(RunSegment.run_id == run.id).all()
        out.update({
            "segments": len(segs),
            "excluded": sum(1 for s in segs if s.status == "excluded"),
            "uncoded": sum(1 for s in segs
                           if s.status not in ("coded", "excluded")),
            "context_window": run.context_window,
            "excluded_roles": json.loads(run.excluded_roles_snapshot)
            if run.excluded_roles_snapshot else [],
            "cost_input_tokens": run.cost_input_tokens,
            "cost_output_tokens": run.cost_output_tokens,
            "documents_status": [
                {"id": rd.document_id, "name": rd.document.display_name,
                 "status": rd.status, "coded_at": _d(rd.coded_at)}
                for rd in run.run_documents],
        })
        tally: dict = {}
        for c in db.query(Coding).filter(Coding.run_id == run.id).all():
            tally[c.code_id] = tally.get(c.code_id, 0) + 1
        labels = {c.id: c.label for c in
                  db.query(Code).filter(Code.workspace_id == run.workspace_id).all()}
        out["codes"] = sorted(
            ({"code_id": cid, "label": labels.get(cid, f"code {cid}"), "codings": n}
             for cid, n in tally.items()), key=lambda r: -r["codings"])
        new_codes = (db.query(Code)
                     .filter(Code.proposed_in_run_id == run.id).all())
        out["proposed_codes"] = [{"id": c.id, "label": c.label,
                                  "description": c.description} for c in new_codes]
        return out
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def get_extracts(code_id: int, run_id: int = 0, limit: int = 20,
                 uncoded: bool = False) -> dict:
    """
    What was actually coded under one code, with the rationale for each assignment.

    This is the validation loop: read the extracts, decide whether the code means
    what you thought it meant, and rewrite the description if it does not. Pass a
    `run_id` to look at one run only, otherwise every run in the workspace.

    `uncoded=True` switches to the opposite question — units the engine passed
    over in that run, with the reason it gave. A code that looks well-populated
    can still be missing half its cases, and only this side shows it. It needs a
    run_id, because "not coded" is a fact about a run, not about a code.
    """
    db = SessionLocal()
    try:
        code = _code_for(db, code_id)
        if uncoded:
            if not run_id:
                return _fail("uncoded=True needs a run_id: a unit is uncoded "
                             "in a run, not in a codebook")
            run = _run_for(db, run_id)
            rows = (db.query(RunSegment)
                    .filter(RunSegment.run_id == run.id,
                            RunSegment.status.notin_(("coded", "excluded")))
                    .limit(limit).all())
            return {"code": code.label, "run": run.id, "mode": "uncoded",
                    "count": len(rows),
                    "segments": [{"document": s.document.display_name,
                                  "speaker": s.speaker, "text": s.segment_text,
                                  "rationale": s.no_code_rationale} for s in rows]}
        q = db.query(Coding).filter(Coding.code_id == code.id)
        if run_id:
            _run_for(db, run_id)
            q = q.filter(Coding.run_id == run_id)
        else:
            ws_runs = [r.id for r in db.query(Run)
                       .filter(Run.workspace_id == code.workspace_id).all()]
            q = q.filter(Coding.run_id.in_(ws_runs))
        rows = q.order_by(Coding.id.desc()).limit(limit).all()
        return {
            "code": code.label, "description": code.description,
            "count": q.count(), "returned": len(rows),
            "extracts": [{"run": c.run_id, "document": c.document.display_name,
                          "speaker": c.speaker, "row": c.row_index,
                          "text": c.segment_text, "rationale": c.rationale,
                          "matched_expressions": json.loads(c.matched_expressions)
                          if c.matched_expressions else None}
                         for c in rows],
        }
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


ANALYSIS_SECTIONS = ("codes", "groups", "cooccurrence", "clusters",
                     "clusters_by_group", "cluster_cooccurrence", "documents",
                     "lemmas", "lemmas_by_code", "expressions", "top_extracts")


@mcp.tool()
def get_analysis(run_id: int, section: str = "", top: int = 25) -> dict:
    """
    The computed analysis of a finished run: code frequencies, group differences,
    co-occurrence, per-document coverage, lemma profiles.

    Computing it runs spaCy over every coded segment, which is minutes on a real
    corpus, so the first call starts the job and returns status "computing" —
    call again to collect. The web analysis page shares the same job, so nothing
    is computed twice.

    Without `section` you get the headline blocks trimmed to `top` rows. With
    one, that block in full. Sections: codes, groups, cooccurrence, clusters,
    clusters_by_group, cluster_cooccurrence, documents, lemmas, lemmas_by_code,
    expressions, top_extracts — the last two only for dictionary runs.
    """
    db = SessionLocal()
    try:
        run = _run_for(db, run_id)
        if run.status in ("pending", "running"):
            return _fail(f"Run {run.id} is still {run.status}")
        if section and section not in ANALYSIS_SECTIONS:
            return _fail(f"Unknown section '{section}'. One of: "
                         f"{', '.join(ANALYSIS_SECTIONS)}")
        if not analysis_mod.is_current(run):
            # An earlier attempt that died has to be *said*, not silently
            # restarted: a job that keeps failing would otherwise report
            # "computing" forever and the caller would poll until it gave up.
            prog = analysis_mod.compute_progress(run)
            if prog.get("status") == "error":
                analysis_mod.forget(run.id)  # so a later call may try again
                return _fail(f"Computing the analysis failed: {prog.get('error')}")
            if analysis_mod.start_compute(run) != "done":
                prog = analysis_mod.compute_progress(run)
                return {"status": "computing", "run": run.id,
                        "done": prog.get("done"), "total": prog.get("total"),
                        "note": "call again in a moment to collect the result"}
        data = analysis_mod.get_analysis(run, run.workspace, db)
        if section:
            return {"run": run.id, "section": section, "data": data.get(section)}
        return {
            "run": run.id, "meta": data.get("meta"),
            "codes": (data.get("codes") or [])[:top],
            "groups": data.get("groups"),
            "cooccurrence": data.get("cooccurrence"),
            "clusters": data.get("clusters"),
            "documents": (data.get("documents") or [])[:top]
            if isinstance(data.get("documents"), list) else data.get("documents"),
            "sections_available": [s for s in ANALYSIS_SECTIONS
                                   if data.get(s) is not None],
        }
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


# ══════════════════════════════════════════════════════════════════════════════
# WRITES
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def create_workspace(name: str, description: str = "", study_context: str = "",
                     input_type: str = "docx") -> dict:
    """
    A new workspace. input_type: docx (transcripts, one file per document) or
    excel (a spreadsheet, one column per document). It is fixed once the corpus
    is no longer empty, so it is worth getting right here.

    `study_context` goes verbatim into the coding system prompt — what the study
    is about, who was interviewed, what the reading is for. The LLM engine
    refuses to start without it, and that refusal is the point: a coder who does
    not know the study is a coder producing plausible noise.
    """
    db = SessionLocal()
    try:
        user = auth.current_caller()
        if not name.strip():
            return _fail("A name is required")
        if input_type not in ("docx", "excel"):
            return _fail("input_type must be docx or excel")
        ws = Workspace(name=name.strip(),
                       description=description.strip() or None,
                       study_context=study_context.strip() or None,
                       owner_id=user.id, input_type=input_type,
                       segmentation_mode="cell" if input_type == "excel"
                       else "utterance_regex")
        db.add(ws)
        db.flush()
        db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id))
        db.commit()
        return {"ok": True, "id": ws.id, "name": ws.name,
                "url": f"/workspace/{ws.id}",
                "next": "upload the corpus from the web app, then add codes"}
    except PermissionError as e:
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def update_workspace(workspace: str, name: str = "", description: str = "",
                     study_context: str = "") -> dict:
    """
    Edit a workspace's name, description or study context. Empty fields are left
    alone, so this can carry one change at a time.

    The input type and the segmentation settings are not here: changing them
    reinterprets a corpus that is already uploaded, and that belongs on a screen
    with a preview next to it.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_workspace(db, workspace, owner=True)
        changed = []
        if name.strip():
            ws.name = name.strip()
            changed.append("name")
        if description.strip():
            ws.description = description.strip()
            changed.append("description")
        if study_context.strip():
            ws.study_context = study_context.strip()
            changed.append("study_context")
        if not changed:
            return {"ok": True, "unchanged": True}
        db.commit()
        return {"ok": True, "id": ws.id, "changed": changed}
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def add_codes(workspace: str, codes: list[dict]) -> dict:
    """
    Add codes to a codebook — one, or a whole draft in a single call.

    Each entry: {"label": required, "cluster": optional family, "description":
    what the LLM interprets, "example": a sample extract, "expressions":
    {"en": ["..."], "de": [...]} for the dictionary engine}. Languages: en, de,
    fr, it.

    A label that already exists is skipped, not merged and not duplicated —
    comparison ignores case, hyphens and underscores, so "risk_perception" and
    "Risk perception" are the same code. The skipped ones come back by name, so
    a second call can rename them rather than guess.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_workspace(db, workspace)
        user = auth.current_caller()
        if not codes:
            return _fail("No codes given")
        existing = {normalize_label(c.label) for c in runs_mod.active_codes(ws, db)}
        created, skipped, bad = [], [], []
        for entry in codes:
            label = str(entry.get("label", "")).strip()
            if not label:
                skipped.append("(no label)")
                continue
            norm = normalize_label(label)
            if norm in existing:
                skipped.append(label)
                continue
            langs = entry.get("expressions") or {}
            unknown = [l for l in langs if l not in SPACY_MODELS]
            if unknown:
                bad.append(f"{label}: unsupported languages {', '.join(unknown)}")
                continue
            code = Code(workspace_id=ws.id, label=label,
                        cluster=(entry.get("cluster") or "").strip() or None,
                        description=(entry.get("description") or "").strip() or None,
                        example=(entry.get("example") or "").strip() or None,
                        created_by_id=user.id, updated_by_id=user.id)
            db.add(code)
            db.flush()
            for lang, exprs in langs.items():
                for expr in dict.fromkeys(e.strip() for e in exprs if e.strip()):
                    db.add(CodeExpression(code_id=code.id, language=lang,
                                          expression=expr))
            existing.add(norm)
            created.append({"id": code.id, "label": label})
        db.commit()
        out = {"ok": True, "created": created, "skipped_existing": skipped,
               "codebook_size": len(runs_mod.active_codes(ws, db))}
        if bad:
            out["rejected"] = bad
        return out
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def update_code(code_id: int, label: str = "", cluster: str = "",
                description: str = "", example: str = "") -> dict:
    """
    Refine one code. Empty fields are left alone; pass "-" to clear a field.

    Refining a description after reading the extracts is the ordinary move, and
    it does not touch past runs: a completed run carries its own codebook
    snapshot, so the history stays readable against the definitions that
    produced it.
    """
    db = SessionLocal()
    try:
        code = _code_for(db, code_id)
        user = auth.current_caller()
        ws = code.workspace
        if label.strip():
            clash = [c for c in runs_mod.active_codes(ws, db)
                     if c.id != code.id
                     and normalize_label(c.label) == normalize_label(label)]
            if clash:
                return _fail(f"'{label}' collides with code {clash[0].id}")
            code.label = label.strip()
        for field, value in (("cluster", cluster), ("description", description),
                             ("example", example)):
            if value.strip() == "-":
                setattr(code, field, None)
            elif value.strip():
                setattr(code, field, value.strip())
        code.updated_by_id = user.id
        code.updated_at = datetime.utcnow()
        db.commit()
        return {"ok": True, **_code_row(code, db)}
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def delete_code(code_id: int) -> dict:
    """
    Retire a code from the codebook. The deletion is soft: codings made in past
    runs keep pointing at it, so old runs stay readable, and only future runs
    stop seeing it.
    """
    db = SessionLocal()
    try:
        code = _code_for(db, code_id)
        user = auth.current_caller()
        n = db.query(Coding).filter(Coding.code_id == code.id).count()
        code.is_deleted = True
        code.updated_by_id = user.id
        code.updated_at = datetime.utcnow()
        db.commit()
        return {"ok": True, "label": code.label, "kept_codings": n}
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def set_expressions(code_id: int, expressions: dict) -> dict:
    """
    Replace a code's dictionary expressions, per language: {"en": ["risk",
    "danger"], "it": [...]}. Languages: en, de, fr, it. An empty list clears one.

    This is a replacement, not a merge — read them with
    get_codebook(with_expressions=True) first if you mean to extend.

    Matching is on lemmas, so one expression covers a word's inflections and
    there is no point listing them. Only the dictionary engine reads these; the
    LLM engine reads the description instead.
    """
    db = SessionLocal()
    try:
        code = _code_for(db, code_id)
        user = auth.current_caller()
        unknown = [l for l in expressions if l not in SPACY_MODELS]
        if unknown:
            return _fail(f"Unsupported languages: {', '.join(unknown)}. "
                         f"One of: {', '.join(sorted(SPACY_MODELS))}")
        db.query(CodeExpression).filter(CodeExpression.code_id == code.id).delete()
        n = 0
        for lang, exprs in expressions.items():
            seen = set()
            for expr in exprs or []:
                expr = str(expr).strip()
                if not expr or expr.lower() in seen:
                    continue
                seen.add(expr.lower())
                db.add(CodeExpression(code_id=code.id, language=lang,
                                      expression=expr))
                n += 1
        code.updated_by_id = user.id
        code.updated_at = datetime.utcnow()
        db.commit()
        return {"ok": True, "label": code.label, "expressions": n}
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def add_member(workspace: str, email: str) -> dict:
    """
    Give a registered colleague access to a workspace — corpus, codebook and run
    history. Owner only, and the person must already have an account here: this
    grants access, it does not send an invitation.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_workspace(db, workspace, owner=True)
        target = (db.query(User)
                  .filter(User.email == email.strip().lower(),
                          User.is_active == True).first())  # noqa: E712
        if not target:
            return _fail(f"No active account for '{email}'")
        if db.query(WorkspaceMember).filter_by(workspace_id=ws.id,
                                               user_id=target.id).first():
            return {"ok": True, "unchanged": True, "member": target.name}
        db.add(WorkspaceMember(workspace_id=ws.id, user_id=target.id))
        db.commit()
        return {"ok": True, "workspace": ws.name, "member": target.name}
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


# ── Runs ──────────────────────────────────────────────────────────────────────

def _spec(document_ids, unit, engine, model, context_window, max_workers,
          excluded_roles) -> runs_mod.RunSpec:
    return runs_mod.RunSpec(document_ids=document_ids, unit=unit or None,
                            engine=engine, model=model,
                            context_window=context_window,
                            max_workers=max_workers,
                            excluded_roles=excluded_roles or [])


@mcp.tool()
def estimate_run(workspace: str, document_ids: list[int], unit: str = "",
                 engine: str = "llm", model: str = "claude-sonnet-4-6",
                 context_window: int = 3, max_workers: int = 5,
                 excluded_roles: list[str] = []) -> dict:
    """
    What a run would cost and how long it would take. Costs nothing to ask.

    Show this before start_run, always: the price scales with the corpus, and a
    number seen after the fact is not a decision. The dictionary engine is free
    and local, so only the segment count is informative there.

    unit — docx: document, utterance_regex, paragraph, sentence; excel: cell,
    sentence. Empty uses the workspace default.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_workspace(db, workspace)
        est = runs_mod.estimate(
            _spec(document_ids, unit, engine, model, context_window,
                  max_workers, excluded_roles), ws, db)
        return {"ok": True, "engine": engine,
                "model": model if engine == "llm" else None, **est}
    except ValueError as e:
        return _fail(str(e))
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def start_run(workspace: str, document_ids: list[int], unit: str = "",
              engine: str = "llm", model: str = "claude-sonnet-4-6",
              context_window: int = 3, max_workers: int = 5,
              excluded_roles: list[str] = []) -> dict:
    """
    Start coding. **This spends the caller's Anthropic credit** — call
    estimate_run first, show the figure, and start only on an explicit yes.

    engine: llm (Claude reads every unit and applies the codebook, with a
    rationale per assignment) or dictionary (deterministic lemma matching, free,
    and nothing leaves the server — the right first choice for a sensitive
    corpus).

    excluded_roles: interviewer, participant, other. Excluding the interviewer
    is the common case, and it is a per-run choice, not a property of the corpus.

    Returns immediately with a run id; the coding continues in the background.
    Poll get_run for progress.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_workspace(db, workspace)
        user = auth.current_caller()
        bad = [r for r in (excluded_roles or []) if r not in conventions.ROLES]
        if bad:
            return _fail(f"Unknown roles: {', '.join(bad)}. "
                         f"One of: {', '.join(conventions.ROLES)}")
        if engine == "llm" and model not in PRICING:
            return _fail(f"Unknown model. One of: {', '.join(sorted(PRICING))}")
        run = runs_mod.launch(
            _spec(document_ids, unit, engine, model, context_window,
                  max_workers, excluded_roles), ws, user, db)
        return {"ok": True, "run_id": run.id, "status": run.status,
                "unit": run.granularity, "engine": run.engine,
                "documents": len(run.run_documents),
                "url": f"/workspace/{ws.id}/runs/{run.id}",
                "note": "running in the background — poll get_run"}
    except ValueError as e:
        return _fail(str(e))
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()
