"""
Autocode web app — FastAPI application.

Phase 1: auth, workspaces + members, corpus upload, codebook CRUD + Excel import,
segmentation preview, profile (API key), admin. Run engine arrives in phase 2.

Patterns: JWT httpOnly cookie (AutoMap v2), bcrypt direct (vedetta),
SQLAlchemy + SQLite, Jinja2 + vanilla JS.
"""
import io
import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

import analysis as analysis_mod
import charts
import coding
import conventions
import dictionary
import exports
import totp
from auth import (create_pending_token, create_token, get_current_user,
                  get_pending_user, get_user_or_none, hash_password, require_admin,
                  verify_password)
from crypto import decrypt_api_key, encrypt_api_key, mask_api_key
from models import (PRICING, Code, CodeExpression, Coding, Document, Run,
                    RunDocument, RunSegment, SessionLocal, User, UserCostLog,
                    Workspace, WorkspaceMember, get_db, init_db, normalize_label,
                    user_total_cost)
from segmentation import SPACY_MODELS, inspect_excel, segment_text
from translations import get_lang, get_t

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "data/uploads"))
INPUT_TYPES = {"docx", "excel"}
DOCX_SEG_MODES = {"document", "utterance_regex", "paragraph", "sentence"}
EXCEL_SEG_MODES = {"cell", "sentence"}

app = FastAPI(title="Autocode")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/imgs", StaticFiles(directory="imgs"), name="imgs")
templates = Jinja2Templates(directory="templates")

init_db()


def _recover_interrupted_runs():
    """Runs left in pending/running by a server restart become retryable."""
    db = next(get_db())
    stuck = db.query(Run).filter(Run.status.in_(("pending", "running"))).all()
    for run in stuck:
        for rd in run.run_documents:
            if rd.status == "pending":
                rd.status = "failed"
        run.status = "failed"
        run.error_message = "Interrupted by a server restart — use 'Retry failed documents'"
        run.completed_at = datetime.utcnow()
    db.commit()


_recover_interrupted_runs()


# ── Template helper ───────────────────────────────────────────────────────────

def render(request: Request, name: str, user: User | None = None, **ctx):
    return templates.TemplateResponse(request, name, {
        "T": get_t(request), "lang": get_lang(request), "user": user, **ctx,
    })


# ── Workspace access helpers ──────────────────────────────────────────────────

def get_workspace_for(user: User, workspace_id: int, db: Session) -> Workspace:
    """Workspace if the user is admin, owner or member; 404 otherwise."""
    ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if user.is_admin or ws.owner_id == user.id:
        return ws
    member = db.query(WorkspaceMember).filter_by(workspace_id=ws.id, user_id=user.id).first()
    if not member:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return ws


def require_owner(user: User, ws: Workspace):
    if not (user.is_admin or ws.owner_id == user.id):
        raise HTTPException(status_code=403, detail="Owner or admin required")


def analyzed_document_ids(ws: Workspace, db: Session) -> set[int]:
    rows = (db.query(RunDocument.document_id)
            .join(Run, Run.id == RunDocument.run_id)
            .filter(Run.workspace_id == ws.id, RunDocument.status == "completed")
            .all())
    return {r[0] for r in rows}


# ══════════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════════

class RegisterIn(BaseModel):
    name: str
    email: str
    password: str


class LoginIn(BaseModel):
    email: str
    password: str


@app.post("/api/auth/register")
def api_register(data: RegisterIn, db: Session = Depends(get_db)):
    email = data.email.strip().lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=400, detail="invalid_email")
    if len(data.password) < 8:
        raise HTTPException(status_code=400, detail="err_pwd_short")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=400, detail="err_email_taken")
    user = User(email=email, name=data.name.strip(), hashed_password=hash_password(data.password))
    db.add(user)
    db.commit()
    # 2FA is mandatory: hand out a pending token and send the user to /2fa to enroll
    resp = JSONResponse({"ok": True, "totp_enabled": False})
    resp.set_cookie("session", create_pending_token(user.id), httponly=True, samesite="lax", max_age=600)
    return resp


@app.post("/api/auth/login")
def api_login(data: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == data.email.strip().lower(), User.is_active == True).first()
    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="err_invalid_login")
    # password ok → pending token; the full session is granted only at the 2FA step
    resp = JSONResponse({"ok": True, "totp_enabled": bool(user.totp_enabled)})
    resp.set_cookie("session", create_pending_token(user.id), httponly=True, samesite="lax", max_age=600)
    return resp


# ── Two-factor (TOTP, mandatory) ──────────────────────────────────────────────

class TotpCodeIn(BaseModel):
    code: str


def _pending_user(request: Request, db: Session) -> User:
    user = get_pending_user(request.cookies.get("session"), db)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired — log in again")
    return user


def _full_session_cookie(resp: JSONResponse, user_id: int):
    resp.set_cookie("session", create_token(user_id), httponly=True, samesite="lax",
                    max_age=7 * 86400)


@app.post("/api/auth/2fa/setup")
def api_2fa_setup(request: Request, db: Session = Depends(get_db)):
    """Generate a secret + QR for enrollment (does not enable 2FA until confirmed)."""
    user = _pending_user(request, db)
    secret = totp.generate_secret()
    db.query(User).filter(User.id == user.id).update(
        {"totp_secret_encrypted": encrypt_api_key(secret)})
    db.commit()
    uri = totp.provisioning_uri(secret, user.email)
    return {"ok": True, "secret": secret, "uri": uri, "qr": totp.qr_data_uri(uri)}


@app.post("/api/auth/2fa/setup/confirm")
def api_2fa_setup_confirm(data: TotpCodeIn, request: Request, db: Session = Depends(get_db)):
    user = _pending_user(request, db)
    if not user.totp_secret_encrypted:
        raise HTTPException(status_code=400, detail="Start the setup first")
    if not totp.verify(decrypt_api_key(user.totp_secret_encrypted), data.code):
        raise HTTPException(status_code=400, detail="Invalid code — check your authenticator app")
    plain, hashes = totp.generate_backup_codes()
    db.query(User).filter(User.id == user.id).update(
        {"totp_enabled": True, "backup_codes_json": json.dumps(hashes)})
    db.commit()
    resp = JSONResponse({"ok": True, "backup_codes": plain})
    _full_session_cookie(resp, user.id)
    return resp


@app.post("/api/auth/2fa/verify")
def api_2fa_verify(data: TotpCodeIn, request: Request, db: Session = Depends(get_db)):
    user = _pending_user(request, db)
    if not (user.totp_enabled and user.totp_secret_encrypted):
        raise HTTPException(status_code=400, detail="2FA is not configured")
    ok = totp.verify(decrypt_api_key(user.totp_secret_encrypted), data.code)
    if not ok:  # fall back to a one-time backup code
        remaining = totp.consume_backup_code(data.code, json.loads(user.backup_codes_json or "[]"))
        if remaining is not None:
            db.query(User).filter(User.id == user.id).update(
                {"backup_codes_json": json.dumps(remaining)})
            db.commit()
            ok = True
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid code")
    resp = JSONResponse({"ok": True})
    _full_session_cookie(resp, user.id)
    return resp


@app.post("/api/auth/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("session")
    return resp


# ══════════════════════════════════════════════════════════════════════════════
# HTML PAGES
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def page_home(request: Request, db: Session = Depends(get_db)):
    user = get_user_or_none(request.cookies.get("session"), db)
    if not user:
        return render(request, "login.html")
    owned = db.query(Workspace).filter(Workspace.owner_id == user.id).all()
    member_rows = (db.query(Workspace)
                   .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
                   .filter(WorkspaceMember.user_id == user.id, Workspace.owner_id != user.id)
                   .all())
    return render(request, "workspaces.html", user,
                  owned=owned, shared=member_rows, spacy_langs=sorted(SPACY_MODELS))


@app.get("/2fa", response_class=HTMLResponse)
def page_2fa(request: Request, db: Session = Depends(get_db)):
    cookie = request.cookies.get("session")
    if get_user_or_none(cookie, db):  # already fully authenticated
        return RedirectResponse("/", status_code=302)
    user = get_pending_user(cookie, db)
    if not user:  # no pending session → back to login
        return RedirectResponse("/", status_code=302)
    return render(request, "twofa.html", None, enrolled=bool(user.totp_enabled), email=user.email)


def _workspace_page(request: Request, workspace_id: int, template: str, db: Session, **extra):
    user = get_user_or_none(request.cookies.get("session"), db)
    if not user:
        return RedirectResponse("/", status_code=302)
    ws = get_workspace_for(user, workspace_id, db)
    return render(request, template, user, ws=ws,
                  is_owner=(user.is_admin or ws.owner_id == user.id), **extra)


@app.get("/workspace/{workspace_id}", response_class=HTMLResponse)
def page_workspace(request: Request, workspace_id: int, db: Session = Depends(get_db)):
    user = get_user_or_none(request.cookies.get("session"), db)
    if not user:
        return RedirectResponse("/", status_code=302)
    ws = get_workspace_for(user, workspace_id, db)
    n_codes = db.query(Code).filter(Code.workspace_id == ws.id, Code.is_deleted == False).count()
    n_runs = db.query(Run).filter(Run.workspace_id == ws.id).count()
    n_groups = len({d.group_label for d in ws.documents if d.group_label})
    members = [{"name": m.user.name, "email": m.user.email, "is_owner": m.user_id == ws.owner_id}
               for m in ws.members]
    return render(request, "workspace_overview.html", user, ws=ws,
                  is_owner=(user.is_admin or ws.owner_id == user.id),
                  n_documents=len(ws.documents), n_codes=n_codes,
                  n_members=len(ws.members), n_runs=n_runs,
                  n_groups=n_groups, members=members)


@app.get("/workspace/{workspace_id}/corpus", response_class=HTMLResponse)
def page_corpus(request: Request, workspace_id: int, db: Session = Depends(get_db)):
    user = get_user_or_none(request.cookies.get("session"), db)
    if not user:
        return RedirectResponse("/", status_code=302)
    ws = get_workspace_for(user, workspace_id, db)
    convention_options = ([(name, p["label"]) for name, p in conventions.PRESETS.items()]
                          + [(n, n) for n in sorted(conventions.workspace_library(ws))])
    return render(request, "workspace_corpus.html", user, ws=ws,
                  is_owner=(user.is_admin or ws.owner_id == user.id),
                  analyzed_ids=analyzed_document_ids(ws, db),
                  convention_options=convention_options)


@app.get("/workspace/{workspace_id}/codebook", response_class=HTMLResponse)
def page_codebook(request: Request, workspace_id: int, db: Session = Depends(get_db)):
    user = get_user_or_none(request.cookies.get("session"), db)
    if not user:
        return RedirectResponse("/", status_code=302)
    ws = get_workspace_for(user, workspace_id, db)
    codes = (db.query(Code)
             .filter(Code.workspace_id == ws.id, Code.is_deleted == False)
             .order_by(Code.label).all())
    counts = dict(
        db.query(Coding.code_id, func.count(Coding.id))
        .join(Run, Run.id == Coding.run_id)
        .filter(Run.workspace_id == ws.id)
        .group_by(Coding.code_id).all())
    return render(request, "workspace_codebook.html", user, ws=ws,
                  is_owner=(user.is_admin or ws.owner_id == user.id),
                  codes=codes, counts=counts, expr_counts=_expression_counts(ws, db))


@app.get("/workspace/{workspace_id}/codes/{code_id}", response_class=HTMLResponse)
def page_code_extracts(request: Request, workspace_id: int, code_id: int,
                       run_id: int | None = None, db: Session = Depends(get_db)):
    user = get_user_or_none(request.cookies.get("session"), db)
    if not user:
        return RedirectResponse("/", status_code=302)
    ws = get_workspace_for(user, workspace_id, db)
    code = db.query(Code).filter(Code.id == code_id, Code.workspace_id == ws.id).first()
    if not code:
        raise HTTPException(status_code=404, detail="Code not found")
    q = (db.query(Coding).join(Run, Run.id == Coding.run_id)
         .filter(Run.workspace_id == ws.id, Coding.code_id == code.id))
    if run_id:
        q = q.filter(Coding.run_id == run_id)
    codings = q.order_by(Coding.document_id, Coding.start_offset).all()
    by_doc: dict = {}
    for c in codings:
        by_doc.setdefault(c.document, []).append(c)
    runs_with_code = (db.query(Run).join(Coding, Coding.run_id == Run.id)
                      .filter(Coding.code_id == code.id).distinct().all())
    return render(request, "code_extracts.html", user, ws=ws,
                  is_owner=(user.is_admin or ws.owner_id == user.id),
                  code=code, by_doc=by_doc, runs_with_code=runs_with_code,
                  selected_run=run_id, n_extracts=len(codings))


@app.get("/workspace/{workspace_id}/settings", response_class=HTMLResponse)
def page_settings(request: Request, workspace_id: int, db: Session = Depends(get_db)):
    user = get_user_or_none(request.cookies.get("session"), db)
    if not user:
        return RedirectResponse("/", status_code=302)
    ws = get_workspace_for(user, workspace_id, db)
    try:
        stoplists = json.loads(ws.stoplists_json or "{}")
    except Exception:
        stoplists = {}
    return render(request, "workspace_settings.html", user, ws=ws,
                  is_owner=(user.is_admin or ws.owner_id == user.id),
                  spacy_langs=sorted(SPACY_MODELS), stoplists=stoplists)


@app.get("/workspace/{workspace_id}/runs/{run_id}/analysis", response_class=HTMLResponse)
def page_run_analysis(request: Request, workspace_id: int, run_id: int,
                      recompute: int = 0, db: Session = Depends(get_db)):
    user = get_user_or_none(request.cookies.get("session"), db)
    if not user:
        return RedirectResponse("/", status_code=302)
    ws = get_workspace_for(user, workspace_id, db)
    run = db.query(Run).filter(Run.id == run_id, Run.workspace_id == ws.id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status in ("pending", "running"):
        raise HTTPException(status_code=400, detail="Run is still in progress")
    if run.status != "completed" and not db.query(Coding).filter(Coding.run_id == run.id).count():
        raise HTTPException(status_code=400, detail="Run has no data")
    # the computation is async (it can be heavy): if there is no cached result, or a
    # recompute was asked, render the progress page — JS kicks off the background
    # compute and reloads when it is done. Otherwise serve the cached analysis.
    data = None
    if not recompute and analysis_mod.is_current(run):
        try:
            data = json.loads(run.analysis_json)
        except Exception:
            data = None
    if data is None:
        return render(request, "run_analysis.html", user, ws=ws, run=run, data=None,
                      is_owner=(user.is_admin or ws.owner_id == user.id),
                      computing=True, recompute=bool(recompute))
    lemma_groups = sorted({c["group"] for cells in data.get("lemmas_by_code_group", {}).values()
                           for c in cells})
    return render(request, "run_analysis.html", user, ws=ws, run=run, data=data,
                  is_owner=(user.is_admin or ws.owner_id == user.id), computing=False,
                  chart_names=charts.available_charts(data), lemma_groups=lemma_groups)


@app.get("/workspace/{workspace_id}/runs", response_class=HTMLResponse)
def page_runs(request: Request, workspace_id: int, db: Session = Depends(get_db)):
    user = get_user_or_none(request.cookies.get("session"), db)
    if not user:
        return RedirectResponse("/", status_code=302)
    ws = get_workspace_for(user, workspace_id, db)
    runs = (db.query(Run).filter(Run.workspace_id == ws.id)
            .order_by(Run.id.desc()).all())
    # dictionary-engine coverage warnings: corpus languages vs codebook expressions
    corpus_langs = sorted({d.language for d in ws.documents if d.language})
    expr_counts = _expression_counts(ws, db)
    n_codes = db.query(Code).filter(Code.workspace_id == ws.id, Code.is_deleted == False).count()
    dict_warnings = []
    for lang in corpus_langs:
        uncovered = n_codes - sum(1 for c in expr_counts.values() if c.get(lang))
        if uncovered:
            dict_warnings.append({"lang": lang, "n": uncovered})
    docs_without_lang = sum(1 for d in ws.documents if not d.language)
    docs_unsegmented = (sum(1 for d in ws.documents
                            if d.source_type == "docx" and not d.convention)
                        if ws.input_type == "docx" else 0)
    return render(request, "workspace_runs.html", user, ws=ws,
                  is_owner=(user.is_admin or ws.owner_id == user.id),
                  runs=runs, analyzed_ids=analyzed_document_ids(ws, db),
                  models=sorted(PRICING), has_api_key=bool(user.api_key_encrypted),
                  dict_warnings=dict_warnings, docs_without_lang=docs_without_lang,
                  docs_unsegmented=docs_unsegmented, n_codes=n_codes,
                  has_expressions=_has_expressions(ws, db))


@app.get("/workspace/{workspace_id}/runs/{run_id}", response_class=HTMLResponse)
def page_run_detail(request: Request, workspace_id: int, run_id: int,
                    db: Session = Depends(get_db)):
    user = get_user_or_none(request.cookies.get("session"), db)
    if not user:
        return RedirectResponse("/", status_code=302)
    ws = get_workspace_for(user, workspace_id, db)
    run = db.query(Run).filter(Run.id == run_id, Run.workspace_id == ws.id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    n_codings = db.query(Coding).filter(Coding.run_id == run.id).count()
    n_new_codes = db.query(Code).filter(Code.proposed_in_run_id == run.id).count()
    n_segments = db.query(RunSegment).filter(RunSegment.run_id == run.id).count()
    n_excluded = (db.query(RunSegment)
                  .filter(RunSegment.run_id == run.id, RunSegment.status == "excluded").count())
    n_uncoded = (db.query(RunSegment)
                 .filter(RunSegment.run_id == run.id,
                         RunSegment.status.notin_(("coded", "excluded"))).count())
    return render(request, "run_detail.html", user, ws=ws,
                  is_owner=(user.is_admin or ws.owner_id == user.id),
                  run=run, n_codings=n_codings, n_new_codes=n_new_codes,
                  n_segments=n_segments, n_uncoded=n_uncoded, n_excluded=n_excluded)


@app.get("/guide", response_class=HTMLResponse)
def page_guide(request: Request):
    import markdown
    md_text = Path("docs/guide.md").read_text(encoding="utf-8")
    body = markdown.markdown(md_text, extensions=["tables", "fenced_code"])
    return render(request, "guide.html", None, guide_html=body)


@app.get("/profile", response_class=HTMLResponse)
def page_profile(request: Request, db: Session = Depends(get_db)):
    user = get_user_or_none(request.cookies.get("session"), db)
    if not user:
        return RedirectResponse("/", status_code=302)
    masked = None
    if user.api_key_encrypted:
        masked = mask_api_key(decrypt_api_key(user.api_key_encrypted))
    cost_rows = (db.query(UserCostLog).filter(UserCostLog.user_id == user.id)
                 .order_by(UserCostLog.recorded_at.desc()).limit(20).all())
    return render(request, "profile.html", user, masked_key=masked,
                  total_cost=user_total_cost(db, user.id), cost_rows=cost_rows)


@app.get("/admin", response_class=HTMLResponse)
def page_admin(request: Request, db: Session = Depends(get_db)):
    user = get_user_or_none(request.cookies.get("session"), db)
    if not user:
        return RedirectResponse("/", status_code=302)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin required")
    users = db.query(User).order_by(User.created_at).all()
    workspaces = db.query(Workspace).order_by(Workspace.created_at.desc()).all()
    costs = {u.id: user_total_cost(db, u.id) for u in users}
    return render(request, "admin.html", user, users=users, workspaces=workspaces, costs=costs)


# ══════════════════════════════════════════════════════════════════════════════
# WORKSPACES API
# ══════════════════════════════════════════════════════════════════════════════

class WorkspaceIn(BaseModel):
    name: str
    description: str | None = None
    study_context: str | None = None
    input_type: str = "docx"
    segmentation_mode: str = "utterance_regex"
    segmentation_regex: str | None = None
    segmentation_language: str | None = None
    stoplists: dict[str, list[str]] | None = None  # per-language lemma stoplists (analysis)


def _seg_default(input_type: str) -> str:
    return "cell" if input_type == "excel" else "utterance_regex"


def _validate_segmentation(data: WorkspaceIn):
    if data.input_type not in INPUT_TYPES:
        raise HTTPException(status_code=400, detail="Invalid input type")
    # the coding unit is chosen per run now (not per workspace); sentence mode uses
    # each document's own language. Only the convention regex is validated here.
    if data.segmentation_regex:
        try:
            re.compile(data.segmentation_regex)
        except re.error as e:
            raise HTTPException(status_code=400, detail=f"Invalid regex: {e}")


@app.post("/api/workspaces")
def api_create_workspace(data: WorkspaceIn, user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    if not data.name.strip():
        raise HTTPException(status_code=400, detail="Name required")
    _validate_segmentation(data)
    ws = Workspace(
        name=data.name.strip(), description=(data.description or "").strip() or None,
        study_context=(data.study_context or "").strip() or None,
        owner_id=user.id, input_type=data.input_type,
        # legacy column: honoured if a valid mode is given (API), else a sane default;
        # the real coding unit is chosen per run, so the UI no longer sets this
        segmentation_mode=(data.segmentation_mode
                           if data.segmentation_mode in (EXCEL_SEG_MODES if data.input_type == "excel"
                                                         else DOCX_SEG_MODES)
                           else _seg_default(data.input_type)),
        segmentation_regex=data.segmentation_regex or None,
        segmentation_language=data.segmentation_language or None,
    )
    db.add(ws)
    db.flush()
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id))  # owner is always a member
    db.commit()
    return {"ok": True, "id": ws.id}


@app.put("/api/workspaces/{workspace_id}")
def api_update_workspace(workspace_id: int, data: WorkspaceIn,
                         user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace_for(user, workspace_id, db)
    require_owner(user, ws)
    if data.input_type != ws.input_type and ws.documents:
        raise HTTPException(status_code=400,
                            detail="Input type can only be changed while the corpus is empty")
    _validate_segmentation(data)
    ws.input_type = data.input_type
    ws.name = data.name.strip() or ws.name
    ws.description = (data.description or "").strip() or None
    ws.study_context = (data.study_context or "").strip() or None
    # segmentation_mode is no longer a workspace setting (chosen per run); reset the
    # legacy default if the input type flips, but still honour a valid explicit mode (API)
    allowed_modes = EXCEL_SEG_MODES if data.input_type == "excel" else DOCX_SEG_MODES
    if data.input_type != ws.input_type:
        ws.segmentation_mode = _seg_default(data.input_type)
    if data.segmentation_mode in allowed_modes:
        ws.segmentation_mode = data.segmentation_mode
    ws.segmentation_regex = data.segmentation_regex or None
    ws.segmentation_language = data.segmentation_language or None
    if data.stoplists is not None:
        bad = [l for l in data.stoplists if l not in SPACY_MODELS]
        if bad:
            raise HTTPException(status_code=400, detail=f"Unsupported languages: {', '.join(bad)}")
        ws.stoplists_json = json.dumps(
            {lang: sorted({t.strip() for t in terms if t.strip()})
             for lang, terms in data.stoplists.items()}, ensure_ascii=False)
    db.commit()
    return {"ok": True}


@app.delete("/api/workspaces/{workspace_id}")
def api_delete_workspace(workspace_id: int, user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    ws = get_workspace_for(user, workspace_id, db)
    require_owner(user, ws)
    ws_dir = UPLOAD_DIR / str(ws.id)
    db.delete(ws)
    db.commit()
    shutil.rmtree(ws_dir, ignore_errors=True)
    return {"ok": True}


class DuplicateIn(BaseModel):
    name: str | None = None
    copy_codebook: bool = False


@app.post("/api/workspaces/{workspace_id}/duplicate")
def api_duplicate_workspace(workspace_id: int, data: DuplicateIn,
                            user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Clone a workspace to reuse the same corpus with a different codebook. Copies
    settings, members and the corpus — physical files included (text files are
    light, and owning them outright keeps the per-workspace unlink logic simple).
    Runs are never copied; the codebook only if the caller opts in.
    """
    src = get_workspace_for(user, workspace_id, db)  # any member may clone
    new = Workspace(
        name=(data.name or "").strip() or f"{src.name} (copy)",
        description=src.description, study_context=src.study_context,
        owner_id=user.id, input_type=src.input_type,
        segmentation_mode=src.segmentation_mode, segmentation_regex=src.segmentation_regex,
        segmentation_language=src.segmentation_language, stoplists_json=src.stoplists_json,
        conventions_json=src.conventions_json, excluded_roles_json=src.excluded_roles_json,
    )
    db.add(new)
    db.flush()
    for uid in {m.user_id for m in src.members} | {user.id}:  # carry the team over
        db.add(WorkspaceMember(workspace_id=new.id, user_id=uid))
    new_dir = UPLOAD_DIR / str(new.id)
    new_dir.mkdir(parents=True, exist_ok=True)
    file_map: dict[str, str] = {}  # old path -> new path (excel columns share one file)
    for doc in src.documents:
        new_path = file_map.get(doc.file_path)
        if new_path is None and doc.file_path and Path(doc.file_path).exists():
            new_path = str(new_dir / f"{uuid4().hex}{Path(doc.file_path).suffix}")
            shutil.copyfile(doc.file_path, new_path)
            file_map[doc.file_path] = new_path
        db.add(Document(workspace_id=new.id, filename=doc.filename,
                        file_path=new_path or "", source_type=doc.source_type,
                        source_config=doc.source_config, language=doc.language,
                        group_label=doc.group_label, convention=doc.convention,
                        roles_json=doc.roles_json, uploaded_by_id=user.id))
    if data.copy_codebook:
        for code in _active_codes(src, db):
            nc = Code(workspace_id=new.id, label=code.label, description=code.description,
                      example=code.example, created_by_id=user.id, updated_by_id=user.id)
            db.add(nc)
            db.flush()
            for e in db.query(CodeExpression).filter(CodeExpression.code_id == code.id).all():
                db.add(CodeExpression(code_id=nc.id, language=e.language, expression=e.expression))
    db.commit()
    return {"ok": True, "id": new.id}


class MemberIn(BaseModel):
    email: str


@app.post("/api/workspaces/{workspace_id}/members")
def api_add_member(workspace_id: int, data: MemberIn,
                   user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace_for(user, workspace_id, db)
    require_owner(user, ws)
    target = db.query(User).filter(User.email == data.email.strip().lower(),
                                   User.is_active == True).first()
    if not target:
        raise HTTPException(status_code=404, detail="No registered user with this email")
    if db.query(WorkspaceMember).filter_by(workspace_id=ws.id, user_id=target.id).first():
        raise HTTPException(status_code=400, detail="Already a member")
    db.add(WorkspaceMember(workspace_id=ws.id, user_id=target.id))
    db.commit()
    return {"ok": True, "name": target.name, "email": target.email}


@app.delete("/api/workspaces/{workspace_id}/members/{member_id}")
def api_remove_member(workspace_id: int, member_id: int,
                      user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace_for(user, workspace_id, db)
    require_owner(user, ws)
    if member_id == ws.owner_id:
        raise HTTPException(status_code=400, detail="Cannot remove the owner")
    row = db.query(WorkspaceMember).filter_by(workspace_id=ws.id, user_id=member_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Not a member")
    db.delete(row)
    db.commit()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# CORPUS API
# ══════════════════════════════════════════════════════════════════════════════

ALLOWED_DOC_EXTS = (".docx", ".txt", ".html", ".htm")


def _detect_document(doc: Document, ws: Workspace, text: str):
    """Language + convention + default roles, at upload or on re-detect."""
    doc.language = dictionary.detect_language(text)
    best = conventions.detect_convention(text, conventions.candidates_for(ws))
    if best:
        doc.convention = best["name"]
        doc.roles_json = json.dumps(conventions.default_roles(best["speakers"]),
                                    ensure_ascii=False)
    else:
        doc.convention = None
        doc.roles_json = None


@app.post("/api/workspaces/{workspace_id}/documents")
def api_upload_documents(workspace_id: int, files: list[UploadFile] = File(...),
                         user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace_for(user, workspace_id, db)
    if ws.input_type != "docx":
        raise HTTPException(status_code=400, detail="This workspace takes Excel files")
    ws_dir = UPLOAD_DIR / str(ws.id)
    ws_dir.mkdir(parents=True, exist_ok=True)
    uploaded, skipped, old_doc = [], [], []
    for f in files:
        name = Path(f.filename or "").name
        suffix = Path(name).suffix.lower()
        if name.lower().endswith(".doc"):  # legacy Word format: explicit, not silent
            old_doc.append(name)
            continue
        # Office temporary lock files (~$...) and unsupported types are skipped
        if suffix not in ALLOWED_DOC_EXTS or name.startswith("~$"):
            skipped.append(name)
            continue
        doc = Document(workspace_id=ws.id, filename=name, file_path="",
                       uploaded_by_id=user.id)
        db.add(doc)
        db.flush()
        dest = ws_dir / f"{doc.id}{suffix}"
        with dest.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        doc.file_path = str(dest)
        try:
            from segmentation import load_document_text
            _detect_document(doc, ws, load_document_text(dest))
        except Exception:
            doc.language = None
            doc.convention = None
        uploaded.append(name)
    db.commit()
    if old_doc:
        raise HTTPException(
            status_code=400,
            detail=f"Legacy .doc format not supported — convert to .docx first: {', '.join(old_doc)}"
            + (f" (uploaded anyway: {', '.join(uploaded)})" if uploaded else ""))
    return {"ok": True, "uploaded": uploaded, "skipped": skipped}


@app.delete("/api/documents/{document_id}")
def api_delete_document(document_id: int, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    ws = get_workspace_for(user, doc.workspace_id, db)
    require_owner(user, ws)
    shared = (db.query(Document)
              .filter(Document.file_path == doc.file_path, Document.id != doc.id).count())
    if not shared:  # excel column-documents share one file: unlink only the last one
        try:
            Path(doc.file_path).unlink(missing_ok=True)
        except OSError:
            pass
    db.delete(doc)
    db.commit()
    return {"ok": True}


# ── Corpus bundle (portable export / import) ──────────────────────────────────

@app.get("/api/workspaces/{workspace_id}/corpus/export")
def api_corpus_export(workspace_id: int, user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    ws = get_workspace_for(user, workspace_id, db)
    return Response(
        exports.export_corpus_bytes(ws, db),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="autocode_corpus_ws{ws.id}.autocorpus"'})


@app.post("/api/workspaces/{workspace_id}/corpus/import")
async def api_corpus_import(workspace_id: int, file: UploadFile = File(...),
                            user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    import zipfile
    ws = get_workspace_for(user, workspace_id, db)
    raw = await file.read()
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        manifest = json.loads(z.read("manifest.json"))
    except Exception:
        raise HTTPException(status_code=400, detail="Not a valid corpus bundle (.autocorpus)")
    if manifest.get("format") != exports.CORPUS_FORMAT:
        raise HTTPException(status_code=400, detail="Not an autocode corpus bundle")
    if manifest.get("input_type") != ws.input_type:
        raise HTTPException(
            status_code=400,
            detail=f"This bundle is '{manifest.get('input_type')}', but the workspace "
                   f"takes '{ws.input_type}' files")
    # merge the bundle's custom conventions into the workspace library (no overwrite)
    lib = conventions.workspace_library(ws)
    for name, regex in (manifest.get("conventions") or {}).items():
        lib.setdefault(name, regex)
    ws.conventions_json = json.dumps(lib, ensure_ascii=False)
    ws_dir = UPLOAD_DIR / str(ws.id)
    ws_dir.mkdir(parents=True, exist_ok=True)
    key_to_path = {}  # dedup: one physical file per bundle key (shared excel workbooks)
    for key, arc in (manifest.get("files") or {}).items():
        dest = ws_dir / f"{uuid4().hex}{Path(arc).suffix}"
        with dest.open("wb") as out:
            out.write(z.read(arc))
        key_to_path[key] = str(dest)
    created = 0
    for d in manifest.get("documents", []):
        db.add(Document(workspace_id=ws.id, filename=d.get("filename") or "imported",
                        file_path=key_to_path.get(d.get("file"), ""),
                        source_type=d.get("source_type", "docx"),
                        source_config=d.get("source_config"), language=d.get("language"),
                        group_label=d.get("group_label"), convention=d.get("convention"),
                        roles_json=d.get("roles_json"), uploaded_by_id=user.id))
        created += 1
    db.commit()
    return {"ok": True, "imported": created}


# ── Excel corpus (input_type = excel) ─────────────────────────────────────────

TMP_TOKEN_RE = re.compile(r"^tmp_[0-9a-f]{32}\.xlsx$")


@app.post("/api/workspaces/{workspace_id}/excel/inspect")
async def api_excel_inspect(workspace_id: int, file: UploadFile = File(...),
                            user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace_for(user, workspace_id, db)
    if ws.input_type != "excel":
        raise HTTPException(status_code=400, detail="This workspace takes DOCX files")
    name = Path(file.filename or "").name
    if not name.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Upload an Excel file (.xlsx)")
    ws_dir = UPLOAD_DIR / str(ws.id)
    ws_dir.mkdir(parents=True, exist_ok=True)
    token = f"tmp_{uuid4().hex}.xlsx"
    dest = ws_dir / token
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    try:
        sheets = inspect_excel(dest)
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Could not read the file: {e}")
    return {"ok": True, "token": token, "filename": name, "sheets": sheets}


class ExcelConfirmIn(BaseModel):
    token: str
    filename: str
    sheet: str
    columns: list[str]
    group_column: str | None = None  # optional per-respondent group (survey condition)


MAX_GROUP_VALUES = 30  # guard against picking a free-text column as the group


@app.post("/api/workspaces/{workspace_id}/excel/confirm")
def api_excel_confirm(workspace_id: int, data: ExcelConfirmIn,
                      user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace_for(user, workspace_id, db)
    if ws.input_type != "excel":
        raise HTTPException(status_code=400, detail="This workspace takes DOCX files")
    if not TMP_TOKEN_RE.match(data.token):
        raise HTTPException(status_code=400, detail="Invalid upload token")
    if not data.columns:
        raise HTTPException(status_code=400, detail="Select at least one column")
    ws_dir = UPLOAD_DIR / str(ws.id)
    tmp = ws_dir / data.token
    if not tmp.exists():
        raise HTTPException(status_code=404, detail="Upload expired — upload the file again")
    sheets = inspect_excel(tmp)
    if data.sheet not in sheets:
        raise HTTPException(status_code=400, detail="Unknown sheet")
    known = {c["name"] for c in sheets[data.sheet]}
    missing = [c for c in data.columns if c not in known]
    if missing:
        raise HTTPException(status_code=400, detail=f"Unknown columns: {', '.join(missing)}")
    if data.group_column and data.group_column not in known:
        raise HTTPException(status_code=400, detail="Unknown group column")
    final = ws_dir / f"xlsx_{uuid4().hex}.xlsx"
    tmp.rename(final)

    from segmentation import load_excel_cells
    # optional per-row group column: split each text column into one document per
    # distinct group value, so the existing per-document group machinery applies
    splits: list[tuple[str | None, str | None]] = [(None, None)]
    if data.group_column:
        gdf = pd.read_excel(final, sheet_name=data.sheet)
        values = sorted({("" if pd.isna(v) else str(v).strip()) for v in gdf[data.group_column]})
        if len(values) > MAX_GROUP_VALUES:
            final.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail=f"'{data.group_column}' has {len(values)} distinct values — "
                       f"pick a categorical column, not a free-text one")
        splits = [(v, v) for v in values]

    created = []
    for col in data.columns:
        for gval, glabel in splits:
            try:
                cells = load_excel_cells(final, data.sheet, col,
                                         data.group_column if gval is not None else None, gval)
            except Exception:
                continue
            if not cells:  # this (column, group) combination has no responses
                continue
            try:
                lang = dictionary.detect_language(" ".join(t for _, t in cells))
            except Exception:
                lang = None
            cfg = {"sheet": data.sheet, "column": col}
            if gval is not None:
                cfg["group_column"] = data.group_column
                cfg["group_value"] = gval
            db.add(Document(workspace_id=ws.id, filename=Path(data.filename).name,
                            file_path=str(final), source_type="excel",
                            source_config=json.dumps(cfg), language=lang,
                            group_label=(glabel or None), uploaded_by_id=user.id))
            created.append(col + (f" ({gval})" if gval else ""))
    db.commit()
    return {"ok": True, "created": created}


class DocumentMetaIn(BaseModel):
    language: str | None = None
    group_label: str | None = None
    convention: str | None = None  # "" clears; preset id or workspace custom name


@app.put("/api/documents/{document_id}")
def api_update_document(document_id: int, data: DocumentMetaIn,
                        user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    ws = get_workspace_for(user, doc.workspace_id, db)
    lang = (data.language or "").strip() or None
    if lang and lang not in SPACY_MODELS:
        raise HTTPException(status_code=400, detail="Unsupported language")
    doc.language = lang
    doc.group_label = (data.group_label or "").strip() or None
    if data.convention is not None:
        name = data.convention.strip() or None
        if name and not conventions.resolve_convention(ws, name):
            raise HTTPException(status_code=400, detail="Unknown convention")
        if name != doc.convention:
            doc.convention = name
            # refresh the speaker inventory for the new convention
            if name:
                try:
                    from segmentation import load_document_text
                    text = load_document_text(doc.file_path)
                    s = conventions.score_convention(
                        [l for l in text.split("\n") if l.strip()][:conventions.SAMPLE_LINES],
                        conventions.resolve_convention(ws, name))
                    doc.roles_json = json.dumps(
                        conventions.default_roles(s["speakers"]) if s else {},
                        ensure_ascii=False)
                except Exception:
                    doc.roles_json = None
            else:
                doc.roles_json = None
    db.commit()
    return {"ok": True}


@app.post("/api/documents/{document_id}/redetect")
def api_redetect_document(document_id: int, user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc or doc.source_type != "docx":
        raise HTTPException(status_code=404, detail="Document not found")
    ws = get_workspace_for(user, doc.workspace_id, db)
    from segmentation import load_document_text
    _detect_document(doc, ws, load_document_text(doc.file_path))
    db.commit()
    return {"ok": True, "convention": doc.convention,
            "roles": json.loads(doc.roles_json or "{}")}


@app.get("/api/documents/{document_id}/sample")
def api_document_sample(document_id: int, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    ws = get_workspace_for(user, doc.workspace_id, db)
    from segmentation import load_document_text
    lines = [l for l in load_document_text(doc.file_path).split("\n") if l.strip()][:30]
    return {"ok": True, "lines": lines, "convention": doc.convention,
            "regex": conventions.resolve_convention(ws, doc.convention),
            "roles": json.loads(doc.roles_json or "{}")}


class RolesIn(BaseModel):
    roles: dict[str, str]


@app.put("/api/documents/{document_id}/roles")
def api_update_roles(document_id: int, data: RolesIn,
                     user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    get_workspace_for(user, doc.workspace_id, db)
    bad = [r for r in data.roles.values() if r not in conventions.ROLES]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown roles: {', '.join(set(bad))}")
    doc.roles_json = json.dumps(
        {conventions.normalize_speaker(k): v for k, v in data.roles.items() if k.strip()},
        ensure_ascii=False)
    db.commit()
    return {"ok": True}


class ConventionIn(BaseModel):
    name: str
    regex: str
    apply_to_document_id: int | None = None


@app.post("/api/workspaces/{workspace_id}/conventions")
def api_add_convention(workspace_id: int, data: ConventionIn,
                       user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace_for(user, workspace_id, db)
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name required")
    if name in conventions.PRESETS:
        raise HTTPException(status_code=400, detail="Name clashes with a built-in preset")
    try:
        conventions.validate_custom_regex(data.regex)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    library = conventions.workspace_library(ws)
    library[name] = data.regex
    ws.conventions_json = json.dumps(library, ensure_ascii=False)
    db.flush()
    applied = None
    if data.apply_to_document_id:
        doc = db.query(Document).filter(Document.id == data.apply_to_document_id,
                                        Document.workspace_id == ws.id).first()
        if doc:
            doc.convention = name
            try:
                from segmentation import load_document_text
                text = load_document_text(doc.file_path)
                s = conventions.score_convention(
                    [l for l in text.split("\n") if l.strip()][:conventions.SAMPLE_LINES],
                    data.regex)
                doc.roles_json = json.dumps(
                    conventions.default_roles(s["speakers"]) if s else {}, ensure_ascii=False)
            except Exception:
                doc.roles_json = None
            applied = doc.id
    db.commit()
    return {"ok": True, "applied_to": applied}


@app.post("/api/documents/{document_id}/suggest-regex")
def api_suggest_regex(document_id: int, user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    get_workspace_for(user, doc.workspace_id, db)
    if not user.api_key_encrypted:
        raise HTTPException(status_code=400, detail="Save your Anthropic API key in the profile first")
    from segmentation import load_document_text
    sample = "\n".join(
        [l for l in load_document_text(doc.file_path).split("\n") if l.strip()][:25])
    try:
        regex = conventions.suggest_regex(decrypt_api_key(user.api_key_encrypted), sample)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"The model's suggestion was unusable: {e}")
    return {"ok": True, "regex": regex}


class BulkGroupIn(BaseModel):
    document_ids: list[int]
    group_label: str | None = None


@app.post("/api/workspaces/{workspace_id}/documents/bulk-group")
def api_bulk_group(workspace_id: int, data: BulkGroupIn,
                   user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace_for(user, workspace_id, db)
    label = (data.group_label or "").strip() or None
    n = (db.query(Document)
         .filter(Document.workspace_id == ws.id, Document.id.in_(data.document_ids))
         .update({"group_label": label}, synchronize_session=False))
    db.commit()
    return {"ok": True, "updated": n}


@app.get("/api/documents/{document_id}/preview-segments")
def api_document_preview(document_id: int, mode: str | None = None,
                         user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    doc = db.query(Document).filter(Document.id == document_id).first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    ws = get_workspace_for(user, doc.workspace_id, db)
    try:
        # the coding unit is a per-run choice; the preview takes it as a parameter
        # (falling back to the legacy default). Role exclusion only marks front matter.
        _, units, _ = coding._units_for_document(ws, doc, mode or ws.segmentation_mode, 0, [])
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    out = [{"row": u["row_index"], "speaker": u["speaker"], "timestamp": u["timestamp"],
            "text": u["text"], "excluded": u["excluded"]}
           for u in units[:12]]
    return {"ok": True, "total": len(units), "segments": out}


class PreviewIn(BaseModel):
    text: str
    mode: str | None = None
    regex: str | None = None


@app.post("/api/workspaces/{workspace_id}/segmentation/preview")
def api_segmentation_preview(workspace_id: int, data: PreviewIn,
                             user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace_for(user, workspace_id, db)
    # excel 'cell' mode previews pasted text one cell per line
    use_mode = data.mode or ws.segmentation_mode
    mode = "paragraph" if use_mode == "cell" else use_mode
    regex = data.regex or ws.segmentation_regex
    try:
        # the paste preview has no document, so sentence mode falls back to a default
        # language (real per-document previews go through _units_for_document)
        segments = segment_text(data.text, mode,
                                regex, ws.segmentation_language or "en")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "segments": segments}


# ══════════════════════════════════════════════════════════════════════════════
# CODEBOOK API
# ══════════════════════════════════════════════════════════════════════════════

class CodeIn(BaseModel):
    label: str
    description: str | None = None
    example: str | None = None
    expressions: dict[str, list[str]] | None = None  # import only


def _active_codes(ws: Workspace, db: Session) -> list[Code]:
    return db.query(Code).filter(Code.workspace_id == ws.id, Code.is_deleted == False).all()


def _has_expressions(ws: Workspace, db: Session) -> bool:
    """Whether the active codebook carries any dictionary expression at all."""
    return bool(db.query(CodeExpression.id)
                .join(Code, Code.id == CodeExpression.code_id)
                .filter(Code.workspace_id == ws.id, Code.is_deleted == False).first())


@app.post("/api/workspaces/{workspace_id}/codes")
def api_add_code(workspace_id: int, data: CodeIn,
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace_for(user, workspace_id, db)
    label = data.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="Label required")
    existing = {normalize_label(c.label) for c in _active_codes(ws, db)}
    if normalize_label(label) in existing:
        raise HTTPException(status_code=400, detail="A code with this label already exists")
    bad = [l for l in (data.expressions or {}) if l not in SPACY_MODELS]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unsupported languages: {', '.join(bad)}")
    code = Code(workspace_id=ws.id, label=label,
                description=(data.description or "").strip() or None,
                example=(data.example or "").strip() or None,
                created_by_id=user.id, updated_by_id=user.id)
    db.add(code)
    db.flush()
    for lang, exprs in (data.expressions or {}).items():
        for expr in dict.fromkeys(e.strip() for e in exprs if e.strip()):
            db.add(CodeExpression(code_id=code.id, language=lang, expression=expr))
    db.commit()
    return {"ok": True, "id": code.id}


@app.put("/api/codes/{code_id}")
def api_update_code(code_id: int, data: CodeIn,
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    code = db.query(Code).filter(Code.id == code_id, Code.is_deleted == False).first()
    if not code:
        raise HTTPException(status_code=404, detail="Code not found")
    ws = get_workspace_for(user, code.workspace_id, db)
    label = data.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="Label required")
    clash = [c for c in _active_codes(ws, db)
             if c.id != code.id and normalize_label(c.label) == normalize_label(label)]
    if clash:
        raise HTTPException(status_code=400, detail="A code with this label already exists")
    code.label = label
    code.description = (data.description or "").strip() or None
    code.example = (data.example or "").strip() or None
    code.updated_by_id = user.id
    code.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@app.delete("/api/codes/{code_id}")
def api_delete_code(code_id: int, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    code = db.query(Code).filter(Code.id == code_id, Code.is_deleted == False).first()
    if not code:
        raise HTTPException(status_code=404, detail="Code not found")
    get_workspace_for(user, code.workspace_id, db)
    code.is_deleted = True  # soft: historical codings keep their reference
    code.updated_by_id = user.id
    code.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@app.get("/api/codes/{code_id}/expressions")
def api_get_expressions(code_id: int, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    code = db.query(Code).filter(Code.id == code_id, Code.is_deleted == False).first()
    if not code:
        raise HTTPException(status_code=404, detail="Code not found")
    get_workspace_for(user, code.workspace_id, db)
    out = {lang: [] for lang in sorted(SPACY_MODELS)}
    for e in db.query(CodeExpression).filter(CodeExpression.code_id == code.id).all():
        out.setdefault(e.language, []).append(e.expression)
    return {"ok": True, "expressions": out}


class ExpressionsIn(BaseModel):
    expressions: dict[str, list[str]]


@app.put("/api/codes/{code_id}/expressions")
def api_set_expressions(code_id: int, data: ExpressionsIn,
                        user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    code = db.query(Code).filter(Code.id == code_id, Code.is_deleted == False).first()
    if not code:
        raise HTTPException(status_code=404, detail="Code not found")
    get_workspace_for(user, code.workspace_id, db)
    bad = [l for l in data.expressions if l not in SPACY_MODELS]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unsupported languages: {', '.join(bad)}")
    db.query(CodeExpression).filter(CodeExpression.code_id == code.id).delete()
    n = 0
    for lang, exprs in data.expressions.items():
        seen = set()
        for expr in exprs:
            expr = expr.strip()
            key = expr.lower()
            if not expr or key in seen:
                continue
            seen.add(key)
            db.add(CodeExpression(code_id=code.id, language=lang, expression=expr))
            n += 1
    code.updated_by_id = user.id
    code.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "count": n}


class ExprPreviewIn(BaseModel):
    expressions: dict[str, list[str]]


@app.post("/api/expressions/preview")
def api_expr_preview(data: ExprPreviewIn, user: User = Depends(get_current_user)):
    """Effective lemma reduction of each expression — what the matcher will actually use."""
    preview: dict = {}
    for lang, exprs in data.expressions.items():
        if lang not in SPACY_MODELS:
            continue
        nlp = dictionary._get_nlp(lang)
        items = []
        for e in exprs:
            if not e.strip():
                continue
            mode, lemmas = dictionary.parse_expression(e, nlp)
            items.append({"expression": e, "mode": mode, "lemmas": lemmas})
        preview[lang] = items
    return {"ok": True, "preview": preview}


def _expression_counts(ws: Workspace, db: Session) -> dict:
    """{code_id: {lang: n}} for the codebook page badges."""
    rows = (db.query(CodeExpression.code_id, CodeExpression.language,
                     func.count(CodeExpression.id))
            .join(Code, Code.id == CodeExpression.code_id)
            .filter(Code.workspace_id == ws.id)
            .group_by(CodeExpression.code_id, CodeExpression.language).all())
    out: dict = {}
    for code_id, lang, n in rows:
        out.setdefault(code_id, {})[lang] = n
    return out


def _parse_codebook_excel(content: bytes) -> list[dict]:
    df = pd.read_excel(io.BytesIO(content))
    cols = {str(c).strip().lower(): c for c in df.columns}
    if "code" not in cols:
        raise HTTPException(status_code=400, detail="Missing 'Code' column")
    rows = []
    for _, r in df.iterrows():
        label = str(r[cols["code"]]).strip()
        if not label or label.lower() == "nan":
            continue
        def _cell(key):
            if key not in cols:
                return None
            v = str(r[cols[key]]).strip()
            return v if v and v.lower() != "nan" else None
        expressions = {}
        for lang in SPACY_MODELS:
            v = _cell(f"expressions_{lang}")
            if v:
                expressions[lang] = [e.strip() for e in v.split(";") if e.strip()]
        rows.append({"label": label, "description": _cell("description"),
                     "example": _cell("example"), "expressions": expressions})
    return rows


@app.get("/api/workspaces/{workspace_id}/codebook/export")
def api_codebook_export(workspace_id: int, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    ws = get_workspace_for(user, workspace_id, db)
    return Response(
        exports.export_codebook_bytes(ws.id, db),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="autocode_codebook_ws{ws.id}.xlsx"'})


@app.post("/api/workspaces/{workspace_id}/codebook/preview-import")
async def api_codebook_preview_import(workspace_id: int, file: UploadFile = File(...),
                                      user: User = Depends(get_current_user),
                                      db: Session = Depends(get_db)):
    ws = get_workspace_for(user, workspace_id, db)
    rows = _parse_codebook_excel(await file.read())
    existing = {normalize_label(c.label) for c in _active_codes(ws, db)}
    seen = set()
    for row in rows:
        norm = normalize_label(row["label"])
        row["duplicate"] = norm in existing or norm in seen
        seen.add(norm)
    return {"ok": True, "rows": rows}


class ImportIn(BaseModel):
    rows: list[CodeIn]


@app.post("/api/workspaces/{workspace_id}/codebook/import")
def api_codebook_import(workspace_id: int, data: ImportIn,
                        user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace_for(user, workspace_id, db)
    existing = {normalize_label(c.label) for c in _active_codes(ws, db)}
    created, skipped = 0, 0
    for row in data.rows:
        label = row.label.strip()
        norm = normalize_label(label)
        if not label or norm in existing:
            skipped += 1
            continue
        code = Code(workspace_id=ws.id, label=label,
                    description=(row.description or "").strip() or None,
                    example=(row.example or "").strip() or None,
                    created_by_id=user.id, updated_by_id=user.id)
        db.add(code)
        db.flush()
        for lang, exprs in (row.expressions or {}).items():
            if lang not in SPACY_MODELS:
                continue
            for expr in dict.fromkeys(e.strip() for e in exprs if e.strip()):
                db.add(CodeExpression(code_id=code.id, language=lang, expression=expr))
        existing.add(norm)
        created += 1
    db.commit()
    return {"ok": True, "created": created, "skipped": skipped}


# ══════════════════════════════════════════════════════════════════════════════
# RUNS API
# ══════════════════════════════════════════════════════════════════════════════

class RunIn(BaseModel):
    document_ids: list[int]
    unit: str | None = None  # coding unit, chosen per run; falls back to the legacy default
    engine: str = "llm"
    context_window: int = 3
    model: str = "claude-sonnet-4-6"
    max_workers: int = 5
    excluded_roles: list[str] = []  # per-run choice: code everything vs exclude e.g. interviewer


def _resolve_run_unit(data: RunIn, ws: Workspace) -> str:
    unit = data.unit or ws.segmentation_mode
    allowed = EXCEL_SEG_MODES if ws.input_type == "excel" else DOCX_SEG_MODES
    if unit not in allowed:
        raise HTTPException(status_code=400,
                            detail=f"Invalid coding unit for this corpus (allowed: {', '.join(sorted(allowed))})")
    return unit


def _validate_run_params(data: RunIn, ws: Workspace, db: Session) -> list[Document]:
    if data.engine not in ("llm", "dictionary"):
        raise HTTPException(status_code=400, detail="Invalid engine")
    bad = [r for r in data.excluded_roles if r not in conventions.ROLES]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown roles: {', '.join(bad)}")
    if data.model not in PRICING:
        raise HTTPException(status_code=400, detail="Unknown model")
    if not (0 <= data.context_window <= 20):
        raise HTTPException(status_code=400, detail="Context window must be 0–20")
    if not (1 <= data.max_workers <= 10):
        raise HTTPException(status_code=400, detail="Max workers must be 1–10")
    docs = (db.query(Document)
            .filter(Document.workspace_id == ws.id, Document.id.in_(data.document_ids))
            .all())
    if not docs:
        raise HTTPException(status_code=400, detail="Select at least one document")
    return docs


@app.post("/api/workspaces/{workspace_id}/runs")
def api_create_run(workspace_id: int, data: RunIn,
                   user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace_for(user, workspace_id, db)
    if data.engine == "llm":  # the dictionary engine needs neither a key nor a study context
        if not user.api_key_encrypted:
            raise HTTPException(status_code=400, detail="Save your Anthropic API key in the profile first")
        if not (ws.study_context or "").strip():
            raise HTTPException(status_code=400,
                                detail="Set the study context in the workspace settings before starting a run")
        # an empty codebook is allowed for the LLM (pure inductive coding); the UI
        # just warns. The dictionary engine, by contrast, can match nothing without
        # expressions — hard block, like the study-context constraint.
    elif data.engine == "dictionary" and not _has_expressions(ws, db):
        raise HTTPException(status_code=400,
                            detail="The dictionary engine needs codes with expressions — your codebook has none. "
                                   "Add expressions in the codebook, or use the LLM engine.")
    # the coding unit is chosen on the run form, snapshotted on the run at launch
    unit = _resolve_run_unit(data, ws)
    if ws.input_type == "excel" or unit == "document" or data.engine == "dictionary":
        data.context_window = 0  # no sequential context for respondents, whole docs or matching
    docs = _validate_run_params(data, ws, db)
    run = Run(workspace_id=ws.id, created_by_id=user.id, status="pending",
              granularity=unit, engine=data.engine, model=data.model,
              context_window=data.context_window, max_workers=data.max_workers,
              excluded_roles_snapshot=json.dumps(sorted(set(data.excluded_roles))))
    db.add(run)
    db.flush()
    for doc in docs:
        db.add(RunDocument(run_id=run.id, document_id=doc.id, status="pending"))
    db.commit()
    threading.Thread(target=coding.execute_run, args=(run.id,), daemon=True).start()
    return {"ok": True, "id": run.id}


@app.get("/api/runs/{run_id}")
def api_run_status(run_id: int, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    get_workspace_for(user, run.workspace_id, db)
    n_codings = db.query(Coding).filter(Coding.run_id == run.id).count()
    n_new_codes = db.query(Code).filter(Code.proposed_in_run_id == run.id).count()
    n_segments = db.query(RunSegment).filter(RunSegment.run_id == run.id).count()
    n_excluded = (db.query(RunSegment)
                  .filter(RunSegment.run_id == run.id, RunSegment.status == "excluded").count())
    n_uncoded = (db.query(RunSegment)
                 .filter(RunSegment.run_id == run.id,
                         RunSegment.status.notin_(("coded", "excluded"))).count())
    # progress + ETA from the documents finished so far (completed or failed are both
    # done); ETA extrapolates the average time per document over those remaining
    n_docs = len(run.run_documents)
    n_done = sum(1 for rd in run.run_documents if rd.status in ("completed", "failed"))
    eta_seconds = None
    if run.status == "running" and run.started_at and 0 < n_done < n_docs:
        elapsed = (datetime.utcnow() - run.started_at).total_seconds()
        eta_seconds = int(elapsed / n_done * (n_docs - n_done))
    return {
        "id": run.id, "status": run.status, "error_message": run.error_message,
        "n_segments": n_segments, "n_uncoded": n_uncoded, "n_excluded": n_excluded,
        "cost_input_tokens": run.cost_input_tokens,
        "cost_output_tokens": run.cost_output_tokens,
        "cost_usd": round(run.cost_usd or 0.0, 4),
        "n_codings": n_codings, "n_new_codes": n_new_codes,
        "n_docs": n_docs, "n_docs_done": n_done, "eta_seconds": eta_seconds,
        "documents": [
            {"id": rd.document_id, "filename": rd.document.display_name, "status": rd.status,
             "coded_at": rd.coded_at.strftime("%H:%M:%S") if rd.coded_at else None}
            for rd in run.run_documents
        ],
    }


@app.post("/api/runs/{run_id}/retry-failed")
def api_retry_failed(run_id: int, user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    ws = get_workspace_for(user, run.workspace_id, db)
    if run.status == "running":
        raise HTTPException(status_code=400, detail="Run is still in progress")
    if run.engine == "llm" and not (ws.study_context or "").strip():
        raise HTTPException(status_code=400,
                            detail="Set the study context in the workspace settings before starting a run")
    if run.engine == "dictionary" and not _has_expressions(ws, db):
        raise HTTPException(status_code=400,
                            detail="The dictionary engine needs codes with expressions — your codebook has none.")
    failed = [rd for rd in run.run_documents if rd.status == "failed"]
    if not failed:
        raise HTTPException(status_code=400, detail="No failed documents to retry")
    if run.engine == "llm" and not user.api_key_encrypted:
        raise HTTPException(status_code=400, detail="Save your Anthropic API key in the profile first")
    for rd in failed:
        rd.status = "pending"
    run.status = "pending"
    run.created_by_id = user.id  # the retrying user pays for the retried documents
    db.commit()
    threading.Thread(target=coding.execute_run, args=(run.id,), daemon=True).start()
    return {"ok": True, "retried": len(failed)}


@app.delete("/api/runs/{run_id}")
def api_delete_run(run_id: int, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    ws = get_workspace_for(user, run.workspace_id, db)
    require_owner(user, ws)
    if run.status in ("pending", "running"):
        raise HTTPException(status_code=400, detail="Run is still in progress — wait for it to finish")
    # codes proposed by this run are part of the codebook now: keep them, just drop
    # the back-reference so it does not dangle to a deleted run
    db.query(Code).filter(Code.proposed_in_run_id == run.id).update(
        {"proposed_in_run_id": None}, synchronize_session=False)
    _analysis_jobs.pop(run.id, None)
    db.delete(run)  # cascades: run_documents, codings, segments, cost_logs
    db.commit()
    return {"ok": True}


def _completed_run_for(user: User, run_id: int, db: Session) -> Run:
    """Run with exportable data: completed, or interrupted/failed but with codings
    (partial results of an interrupted run are legitimate data)."""
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    get_workspace_for(user, run.workspace_id, db)
    if run.status in ("pending", "running"):
        raise HTTPException(status_code=400, detail="Run is still in progress")
    if run.status != "completed" and not db.query(Coding).filter(Coding.run_id == run.id).count():
        raise HTTPException(status_code=400, detail="Run has no data")
    return run


@app.get("/api/runs/{run_id}/export/xlsx")
def api_export_xlsx(run_id: int, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    run = _completed_run_for(user, run_id, db)
    return Response(
        exports.export_xlsx_bytes(run, db),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="autocode_run{run.id}.xlsx"'})


@app.get("/api/runs/{run_id}/export/qdc")
def api_export_qdc(run_id: int, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    run = _completed_run_for(user, run_id, db)
    codes = (db.query(Code)
             .filter(Code.workspace_id == run.workspace_id, Code.is_deleted == False)
             .order_by(Code.label).all())
    return Response(
        exports.export_qdc_bytes(codes),
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="autocode_codebook_ws{run.workspace_id}.qdc"'})


@app.get("/api/runs/{run_id}/export/qdpx")
def api_export_qdpx(run_id: int, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    run = _completed_run_for(user, run_id, db)
    return Response(
        exports.export_qdpx_bytes(run, db),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="autocode_run{run.id}.qdpx"'})


@app.get("/api/runs/{run_id}/analysis/chart/{name}")
def api_analysis_chart(run_id: int, name: str, fmt: str = "png", theme: str = "dark",
                       download: int = 0, code: str | None = None, group: str | None = None,
                       user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    run = _completed_run_for(user, run_id, db)
    if fmt not in ("png", "pdf") or theme not in charts.THEMES:
        raise HTTPException(status_code=400, detail="Invalid format or theme")
    data = analysis_mod.get_analysis(run, run.workspace, db)
    img = charts.render_chart(name, data, theme=theme, fmt=fmt, code=code, group=group)
    if img is None:
        raise HTTPException(status_code=404, detail="No data for this chart")
    headers = {}
    if download:
        suffix = f"_{code}" if code else ""
        headers["Content-Disposition"] = \
            f'attachment; filename="autocode_run{run.id}_{name}{suffix}.{fmt}"'
    return Response(img, media_type="application/pdf" if fmt == "pdf" else "image/png",
                    headers=headers)


@app.get("/api/runs/{run_id}/analysis/export")
def api_analysis_export(run_id: int, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    run = _completed_run_for(user, run_id, db)
    data = analysis_mod.get_analysis(run, run.workspace, db)
    return Response(
        exports.export_analysis_bytes(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="autocode_run{run.id}_analysis.xlsx"'})


# ── Async analysis compute (heavy: the lemma loop runs spaCy per coded segment) ──
_analysis_jobs: dict[int, dict] = {}
_analysis_lock = threading.Lock()


def _compute_analysis_bg(run_id: int, recompute: bool, progress: dict):
    db = SessionLocal()
    try:
        run = db.query(Run).filter(Run.id == run_id).first()
        ws = db.query(Workspace).filter(Workspace.id == run.workspace_id).first() if run else None
        if not run or not ws:
            progress["status"] = "error"
            progress["error"] = "Run not found"
            return
        analysis_mod.get_analysis(run, ws, db, recompute=recompute, progress=progress)
        progress["status"] = "done"
    except Exception as e:
        progress["status"] = "error"
        progress["error"] = str(e)
    finally:
        db.close()


@app.post("/api/runs/{run_id}/analysis/compute")
def api_analysis_compute(run_id: int, recompute: int = 0,
                         user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    run = _completed_run_for(user, run_id, db)
    if not recompute and analysis_mod.is_current(run):
        return {"ok": True, "status": "done"}
    with _analysis_lock:
        job = _analysis_jobs.get(run_id)
        if job and job.get("status") == "running":
            return {"ok": True, "status": "running"}
        progress = {"status": "running", "total": 0, "done": 0, "error": None}
        _analysis_jobs[run_id] = progress
    threading.Thread(target=_compute_analysis_bg,
                     args=(run_id, bool(recompute), progress), daemon=True).start()
    return {"ok": True, "status": "running"}


@app.get("/api/runs/{run_id}/analysis/progress")
def api_analysis_progress(run_id: int, user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    run = db.query(Run).filter(Run.id == run_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    get_workspace_for(user, run.workspace_id, db)
    job = _analysis_jobs.get(run_id)
    if not job:
        return {"status": "done" if analysis_mod.is_current(run) else "idle",
                "total": 0, "done": 0}
    return {"status": job.get("status"), "total": job.get("total", 0),
            "done": job.get("done", 0), "error": job.get("error")}


class EstimateIn(BaseModel):
    document_ids: list[int]
    unit: str | None = None
    engine: str = "llm"
    context_window: int = 3
    model: str = "claude-sonnet-4-6"
    max_workers: int = 5
    excluded_roles: list[str] = []


@app.post("/api/workspaces/{workspace_id}/runs/estimate")
def api_estimate_run(workspace_id: int, data: EstimateIn,
                     user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace_for(user, workspace_id, db)
    run_in = RunIn(**data.model_dump())
    docs = _validate_run_params(run_in, ws, db)
    unit = _resolve_run_unit(run_in, ws)
    ctx = 0 if (ws.input_type == "excel" or unit == "document") else data.context_window
    codes = (db.query(Code)
             .filter(Code.workspace_id == ws.id, Code.is_deleted == False).all())
    est = coding.estimate_run_cost(ws, docs, unit, ctx, data.model, codes,
                                   excluded_roles=data.excluded_roles)
    if data.engine == "dictionary":  # free and local: only the segment count is informative
        est.update({"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
    est["eta_seconds"] = coding.estimate_run_seconds(
        data.engine, est["segments"], data.max_workers)
    return {"ok": True, **est}


# ══════════════════════════════════════════════════════════════════════════════
# PROFILE API
# ══════════════════════════════════════════════════════════════════════════════

class ApiKeyIn(BaseModel):
    api_key: str


@app.post("/api/profile/api-key")
def api_save_key(data: ApiKeyIn, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    key = data.api_key.strip()
    if not key.startswith("sk-"):
        raise HTTPException(status_code=400, detail="That does not look like an Anthropic API key")
    db.query(User).filter(User.id == user.id).update(
        {"api_key_encrypted": encrypt_api_key(key)})
    db.commit()
    return {"ok": True, "masked": mask_api_key(key)}


@app.delete("/api/profile/api-key")
def api_remove_key(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.query(User).filter(User.id == user.id).update({"api_key_encrypted": None})
    db.commit()
    return {"ok": True}


@app.post("/api/profile/2fa/backup-codes")
def api_regen_backup_codes(data: TotpCodeIn, user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    """Regenerate the backup codes (invalidates the old ones); needs a current code."""
    if not (user.totp_enabled and user.totp_secret_encrypted):
        raise HTTPException(status_code=400, detail="2FA is not configured")
    if not totp.verify(decrypt_api_key(user.totp_secret_encrypted), data.code):
        raise HTTPException(status_code=400, detail="Invalid code")
    plain, hashes = totp.generate_backup_codes()
    db.query(User).filter(User.id == user.id).update({"backup_codes_json": json.dumps(hashes)})
    db.commit()
    return {"ok": True, "backup_codes": plain}


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN API
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/admin/users/{user_id}/toggle-active")
def api_toggle_user(user_id: int, admin: User = Depends(require_admin),
                    db: Session = Depends(get_db)):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot disable yourself")
    target.is_active = not target.is_active
    db.commit()
    return {"ok": True, "is_active": target.is_active}


@app.post("/api/admin/users/{user_id}/reset-2fa")
def api_admin_reset_2fa(user_id: int, admin: User = Depends(require_admin),
                        db: Session = Depends(get_db)):
    """Clear a user's 2FA so they re-enroll on next login (lost device recovery)."""
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    db.query(User).filter(User.id == user_id).update(
        {"totp_enabled": False, "totp_secret_encrypted": None, "backup_codes_json": None})
    db.commit()
    return {"ok": True}
