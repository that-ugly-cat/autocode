"""
Database models for Autocode web app.

ORM: SQLAlchemy with SQLite (single-file DB at ./data/autocode.db, persisted via Docker volume).

Entities follow the spec in wiki/projects/strumenti/autocode-webapp.md:
  User, Workspace, WorkspaceMember, Document, Code, Run, RunDocument, Coding, UserCostLog

Code deletion is soft (is_deleted flag): historical Codings keep pointing to deleted codes
so past runs remain readable.

Migration strategy: init_db() runs ALTER TABLE for each new column on every startup.
SQLite raises on duplicate columns; failures are caught and ignored (additive only).
"""
import os
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text,
    create_engine, func, select,
)
from sqlalchemy.orm import DeclarativeBase, backref, relationship, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/autocode.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

DEFAULT_UTTERANCE_REGEX = r"^(.+?) \[(\d{2}:\d{2}:\d{2})\]: (.+)$"


class Base(DeclarativeBase):
    pass


# ── Users ─────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"
    id                = Column(Integer, primary_key=True)
    email             = Column(String, unique=True, nullable=False)
    name              = Column(String, nullable=False)
    hashed_password   = Column(String, nullable=False)
    api_key_encrypted = Column(String, nullable=True)   # Anthropic key, Fernet-encrypted
    totp_secret_encrypted = Column(String, nullable=True)   # TOTP secret, Fernet-encrypted
    totp_enabled      = Column(Boolean, default=False)
    backup_codes_json = Column(Text, nullable=True)     # sha256 hashes of unused backup codes
    is_admin          = Column(Boolean, default=False)
    is_active         = Column(Boolean, default=True)
    created_at        = Column(DateTime, default=datetime.utcnow)

    owned_workspaces = relationship("Workspace", back_populates="owner")


# ── Workspaces ────────────────────────────────────────────────────────────────

class Workspace(Base):
    __tablename__ = "workspaces"
    id                    = Column(Integer, primary_key=True)
    name                  = Column(String, nullable=False)
    description           = Column(Text, nullable=True)
    study_context         = Column(Text, nullable=True)   # goes into the coding system prompt
    owner_id              = Column(Integer, ForeignKey("users.id"), nullable=False)
    input_type            = Column(String, default="docx")  # docx | excel — fixed once corpus is non-empty
    segmentation_mode     = Column(String, default="utterance_regex")  # docx: utterance_regex | paragraph | sentence; excel: cell | sentence
    segmentation_regex    = Column(String, nullable=True, default=DEFAULT_UTTERANCE_REGEX)
    segmentation_language = Column(String, nullable=True)              # en | de | fr | it (sentence mode)
    stoplists_json        = Column(Text, nullable=True)  # per-language custom lemma stoplists for the analysis page
    conventions_json      = Column(Text, nullable=True)  # custom transcript conventions library {name: regex}
    excluded_roles_json   = Column(Text, nullable=True)  # roles excluded from coding, e.g. ["interviewer"]
    created_at            = Column(DateTime, default=datetime.utcnow)

    owner     = relationship("User", back_populates="owned_workspaces")
    members   = relationship("WorkspaceMember", back_populates="workspace", cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="workspace", cascade="all, delete-orphan")
    codes     = relationship("Code", back_populates="workspace", cascade="all, delete-orphan")
    runs      = relationship("Run", back_populates="workspace", cascade="all, delete-orphan")


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), primary_key=True)
    user_id      = Column(Integer, ForeignKey("users.id"), primary_key=True)

    workspace = relationship("Workspace", back_populates="members")
    user      = relationship("User")


# ── Documents ─────────────────────────────────────────────────────────────────

class Document(Base):
    __tablename__ = "documents"
    id             = Column(Integer, primary_key=True)
    workspace_id   = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    filename       = Column(String, nullable=False)   # original name
    file_path      = Column(String, nullable=False)   # server path; shared between excel column-documents
    source_type    = Column(String, default="docx")   # docx | excel
    source_config  = Column(Text, nullable=True)      # excel: JSON {"sheet": ..., "column": ...}
    language       = Column(String, nullable=True)    # en|de|fr|it — auto-detected at upload, editable
    group_label    = Column(String, nullable=True)    # optional analytics grouping (FINK "module")
    convention     = Column(String, nullable=True)    # transcript convention (preset id or custom name); None = unsegmented
    roles_json     = Column(Text, nullable=True)      # speaker label → role mapping {"I": "interviewer", ...}
    uploaded_at    = Column(DateTime, default=datetime.utcnow)
    uploaded_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    @property
    def display_name(self) -> str:
        if self.source_type == "excel" and self.source_config:
            import json
            cfg = json.loads(self.source_config)
            base = f"{self.filename} [{cfg.get('column', '')}]"
            gv = cfg.get("group_value")
            return f"{base} ({gv})" if gv else base
        return self.filename

    workspace   = relationship("Workspace", back_populates="documents")
    uploaded_by = relationship("User")
    # deleting a document removes its run links, codings and coverage rows:
    # without these cascades SQLite id reuse can graft orphans onto new runs
    run_links    = relationship("RunDocument", back_populates="document",
                                cascade="all, delete-orphan")
    codings      = relationship("Coding", back_populates="document",
                                cascade="all, delete-orphan")
    run_segments = relationship("RunSegment", back_populates="document",
                                cascade="all, delete-orphan")


# ── Codes ─────────────────────────────────────────────────────────────────────

class Code(Base):
    __tablename__ = "codes"
    id                = Column(Integer, primary_key=True)
    workspace_id      = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    label             = Column(String, nullable=False)
    description       = Column(Text, nullable=True)
    example           = Column(Text, nullable=True)
    is_model_proposed = Column(Boolean, default=False)
    proposed_in_run_id = Column(Integer, ForeignKey("runs.id"), nullable=True)
    is_deleted        = Column(Boolean, default=False)   # soft delete: historical codings survive
    created_at        = Column(DateTime, default=datetime.utcnow)
    created_by_id     = Column(Integer, ForeignKey("users.id"), nullable=True)  # null = model
    updated_at        = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by_id     = Column(Integer, ForeignKey("users.id"), nullable=True)

    workspace = relationship("Workspace", back_populates="codes")


class CodeExpression(Base):
    """
    Dictionary-engine expressions attached to a code, per language (FINK heritage).
    Relational (not JSON on Code) so the engine can validate language coverage at
    launch and per-expression match stats stay queryable.
    """
    __tablename__ = "code_expressions"
    id         = Column(Integer, primary_key=True)
    code_id    = Column(Integer, ForeignKey("codes.id"), nullable=False)
    language   = Column(String, nullable=False)   # en | de | fr | it
    expression = Column(String, nullable=False)

    code = relationship("Code",
                        backref=backref("code_expressions", cascade="all, delete-orphan"))


def normalize_label(label: str) -> str:
    """Canonical form used for duplicate detection at insert time."""
    return " ".join(label.lower().replace("_", " ").replace("-", " ").split())


# ── Runs ──────────────────────────────────────────────────────────────────────

class Run(Base):
    __tablename__ = "runs"
    id                     = Column(Integer, primary_key=True)
    workspace_id           = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    created_by_id          = Column(Integer, ForeignKey("users.id"), nullable=False)
    status                 = Column(String, default="pending")  # pending | running | completed | failed
    # coding unit snapshotted from the workspace at launch, so runs are immune to
    # later settings changes: document | utterance_regex | paragraph | sentence | cell
    # (legacy values per_row / per_utterance are mapped in coding._resolve_unit)
    granularity            = Column(String, default="per_utterance")
    engine                 = Column(String, default="llm")  # llm | dictionary
    model                  = Column(String, default="claude-sonnet-4-6")
    context_window         = Column(Integer, default=3)
    max_workers            = Column(Integer, default=5)
    qdpx_enabled           = Column(Boolean, default=True)  # legacy column: QDPX is always available now
    codebook_snapshot_json = Column(Text, nullable=True)  # audit: codebook at run start
    excluded_roles_snapshot = Column(Text, nullable=True)  # audit: roles excluded at launch
    started_at             = Column(DateTime, nullable=True)
    completed_at           = Column(DateTime, nullable=True)
    error_message          = Column(Text, nullable=True)
    cost_input_tokens      = Column(Integer, default=0)
    cost_output_tokens     = Column(Integer, default=0)
    cost_usd               = Column(Float, default=0.0)
    analysis_json          = Column(Text, nullable=True)  # cached analysis (runs are immutable once completed)

    workspace     = relationship("Workspace", back_populates="runs")
    created_by    = relationship("User")
    run_documents = relationship("RunDocument", back_populates="run", cascade="all, delete-orphan")
    codings       = relationship("Coding", back_populates="run", cascade="all, delete-orphan")
    segments      = relationship("RunSegment", cascade="all, delete-orphan")
    cost_logs     = relationship("UserCostLog", back_populates="run", cascade="all, delete-orphan")


class RunDocument(Base):
    __tablename__ = "run_documents"
    run_id      = Column(Integer, ForeignKey("runs.id"), primary_key=True)
    document_id = Column(Integer, ForeignKey("documents.id"), primary_key=True)
    status      = Column(String, default="pending")  # pending | completed | failed
    coded_at    = Column(DateTime, nullable=True)

    run      = relationship("Run", back_populates="run_documents")
    document = relationship("Document", back_populates="run_links")


class RunSegment(Base):
    """
    Every unit the model processed in a run, coded or not — the complete photograph.
    Codings only persist the positive half; this table keeps coverage and the
    model's no_code rationales (negative cases worth human review).
    """
    __tablename__ = "run_segments"
    id                = Column(Integer, primary_key=True)
    run_id            = Column(Integer, ForeignKey("runs.id"), nullable=False)
    document_id       = Column(Integer, ForeignKey("documents.id"), nullable=False)
    position          = Column(Integer, nullable=False)  # order within the document
    segment_text      = Column(Text, nullable=False)
    start_offset      = Column(Integer, nullable=True)
    end_offset        = Column(Integer, nullable=True)
    row_index         = Column(Integer, nullable=True)   # excel: spreadsheet row
    speaker           = Column(String, nullable=True)    # normalized speaker label (utterance docs)
    status            = Column(String, default="no_code")  # coded | no_code | error | excluded
    no_code_rationale = Column(Text, nullable=True)

    document = relationship("Document", back_populates="run_segments")


class Coding(Base):
    __tablename__ = "codings"
    id           = Column(Integer, primary_key=True)
    run_id       = Column(Integer, ForeignKey("runs.id"), nullable=False)
    document_id  = Column(Integer, ForeignKey("documents.id"), nullable=False)
    code_id      = Column(Integer, ForeignKey("codes.id"), nullable=False)
    segment_text = Column(Text, nullable=False)
    start_offset = Column(Integer, nullable=True)  # char offsets (per_utterance docx + excel)
    end_offset   = Column(Integer, nullable=True)
    row_index    = Column(Integer, nullable=True)  # excel: 1-based spreadsheet row of the cell
    speaker      = Column(String, nullable=True)   # normalized speaker label (utterance docs)
    rationale    = Column(Text, nullable=True)
    matched_expressions = Column(Text, nullable=True)   # dictionary engine: JSON list
    relevance_score     = Column(Integer, nullable=True)  # dictionary engine: matches count
    created_at   = Column(DateTime, default=datetime.utcnow)

    run      = relationship("Run", back_populates="codings")
    code     = relationship("Code")
    document = relationship("Document", back_populates="codings")


# ── Cost tracking ─────────────────────────────────────────────────────────────

# Pricing per million tokens (input, output) — update when Anthropic changes rates
PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6": (3.0,  15.0),
    "claude-opus-4-8":   (15.0, 75.0),
    "claude-haiku-4-5":  (0.8,   4.0),
}


def calc_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    p = PRICING.get(model, PRICING["claude-sonnet-4-6"])
    return (tokens_in * p[0] + tokens_out * p[1]) / 1_000_000


class UserCostLog(Base):
    __tablename__ = "user_cost_log"
    id            = Column(Integer, primary_key=True)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=False)
    run_id        = Column(Integer, ForeignKey("runs.id"), nullable=False)
    input_tokens  = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cost_usd      = Column(Float, default=0.0)
    recorded_at   = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")
    run  = relationship("Run", back_populates="cost_logs")


# ── Init / helpers ────────────────────────────────────────────────────────────

def init_db():
    os.makedirs("data", exist_ok=True)
    Base.metadata.create_all(bind=engine)
    # Additive migrations: attempted on every startup, duplicates ignored.
    from sqlalchemy import text
    with engine.connect() as conn:
        for stmt in [
            "ALTER TABLE workspaces ADD COLUMN study_context TEXT",
            "ALTER TABLE workspaces ADD COLUMN input_type VARCHAR DEFAULT 'docx'",
            "ALTER TABLE documents ADD COLUMN source_type VARCHAR DEFAULT 'docx'",
            "ALTER TABLE documents ADD COLUMN source_config TEXT",
            "ALTER TABLE documents ADD COLUMN language VARCHAR",
            "ALTER TABLE documents ADD COLUMN group_label VARCHAR",
            "ALTER TABLE runs ADD COLUMN engine VARCHAR DEFAULT 'llm'",
            "ALTER TABLE runs ADD COLUMN analysis_json TEXT",
            "ALTER TABLE workspaces ADD COLUMN stoplists_json TEXT",
            "ALTER TABLE workspaces ADD COLUMN conventions_json TEXT",
            "ALTER TABLE workspaces ADD COLUMN excluded_roles_json TEXT",
            "ALTER TABLE documents ADD COLUMN convention VARCHAR",
            "ALTER TABLE documents ADD COLUMN roles_json TEXT",
            "ALTER TABLE runs ADD COLUMN excluded_roles_snapshot TEXT",
            "ALTER TABLE run_segments ADD COLUMN speaker VARCHAR",
            "ALTER TABLE codings ADD COLUMN speaker VARCHAR",
            "ALTER TABLE codings ADD COLUMN row_index INTEGER",
            "ALTER TABLE codings ADD COLUMN matched_expressions TEXT",
            "ALTER TABLE codings ADD COLUMN relevance_score INTEGER",
            "ALTER TABLE users ADD COLUMN totp_secret_encrypted VARCHAR",
            "ALTER TABLE users ADD COLUMN totp_enabled BOOLEAN DEFAULT 0",
            "ALTER TABLE users ADD COLUMN backup_codes_json TEXT",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def user_total_cost(db, user_id: int) -> float:
    result = db.execute(
        select(func.coalesce(func.sum(UserCostLog.cost_usd), 0.0))
        .where(UserCostLog.user_id == user_id)
    ).scalar()
    return float(result)
