"""
Run engine for Autocode web app — ported from tools/autocode/autocode.ipynb.

Execution model:
- one background thread per Run (execute_run), documents processed sequentially;
- segments within a document are coded in parallel (ThreadPoolExecutor, run.max_workers);
- worker threads only call the API; all DB writes happen in the run thread.

New-code dedup (three layers, decided June 2026):
1. insert-time: proposed labels are normalized and matched case-insensitively against
   the active codebook (including codes created earlier in this run) — exact duplicates
   map onto the existing code instead of creating one;
2. codebook reload at document boundaries: each document rebuilds the system prompt
   from the current DB codebook, so later documents see codes proposed by earlier ones
   (one prompt-cache write per codebook version, amortized over the document's segments);
3. residual near-duplicates stay visible in the codebook UI with the model badge.
"""
import hashlib
import json
import re
import time
import traceback
from typing import NamedTuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import anthropic

import conventions
import dictionary
from crypto import decrypt_api_key
from models import (MODEL_PROFILES, Code, Coding, Run, RunDocument, RunSegment,
                    SessionLocal, User, UserCostLog, Workspace, calc_cost, normalize_label)
from segmentation import (excel_fulltext, load_document_text, load_excel_cells,
                          segment_text, split_sentences, split_utterances)

# The system prompt has three parts with three owners: the study context and the coding
# instructions belong to the workspace, the codebook to its codes, and the reply format
# to the engine. A workspace that sets coding instructions replaces DEFAULT_CODING_RULES
# and nothing else: the reply format stays the engine's, because the parser depends on it.
DEFAULT_CODING_RULES = """\
- Assign one or more existing codes if they meaningfully apply.
- Propose a new code only if no existing code captures an important theme.
- Leave the excerpt uncoded if it contains no content relevant to the study.
- A single excerpt may contain multiple themes — code all of them if clearly supported."""

SYSTEM_PROMPT_TEMPLATE = """\
You are an expert qualitative researcher conducting abductive thematic analysis.

Study context:
{context}

Your task is to analyze text excerpts and assign qualitative codes from the codebook below.

Coding instructions:
{instructions}

Codebook:
{codebook}

Reply with a JSON object {{"codings": [...]}}. Each element of "codings" has five fields:
  "action": "use_existing", "create_new" or "no_code"
  "code": the code label ("" for no_code)
  "description": a short definition, for create_new only ("" otherwise)
  "example": the excerpt that motivates it, for create_new only ("" otherwise)
  "rationale": why
Use "no_code" with a rationale when the excerpt carries nothing relevant to the study.
Return {{"codings": []}} only if the text is completely uninformative."""

# The reply schema for models that support structured outputs. Every field is required
# and unused ones are empty strings: the flat shape keeps the schema free of unions.
CODINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "codings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["use_existing", "create_new", "no_code"]},
                    "code": {"type": "string"},
                    "description": {"type": "string"},
                    "example": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["action", "code", "description", "example", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["codings"],
    "additionalProperties": False,
}

EST_OUTPUT_TOKENS_PER_SEGMENT = 150   # rough average for the JSON response
# thinking at effort "low", billed as output: a guess until a real run measures it
EST_THINKING_TOKENS_PER_SEGMENT = 400
EST_PROMPT_OVERHEAD_TOKENS = 50       # message scaffolding around the excerpt

# Rough run-time estimate (deliberately approximate — the live progress bar refines
# it). LLM: each segment is one API round-trip, run max_workers in parallel.
# Dictionary: a spaCy pass per unit, plus a one-off model-load overhead per run.
EST_SEC_PER_LLM_CALL = 4.0
EST_SEC_PER_DICT_UNIT = 0.012
EST_DICT_LOAD_OVERHEAD = 8.0


def estimate_run_seconds(engine: str, n_segments: int, max_workers: int) -> int:
    """Ballpark wall-clock seconds for a run, for the pre-run estimate."""
    if n_segments <= 0:
        return 0
    if engine == "dictionary":
        return int(EST_DICT_LOAD_OVERHEAD + n_segments * EST_SEC_PER_DICT_UNIT)
    workers = max(1, max_workers)
    waves = -(-n_segments // workers)  # ceil division
    return int(waves * EST_SEC_PER_LLM_CALL)


# ── Prompt building ───────────────────────────────────────────────────────────

def format_codebook(codes: list[Code]) -> str:
    if not codes:
        return "(the codebook is currently empty — propose codes as needed)"
    return "\n".join(f"- **{c.label}**: {c.description or ''}".rstrip() for c in codes)


def build_system_prompt(study_context: str, codes: list[Code],
                        coding_instructions: str | None = None) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        context=(study_context or "").strip() or "(no study context provided)",
        instructions=(coding_instructions or "").strip() or DEFAULT_CODING_RULES,
        codebook=format_codebook(codes),
    )


def workspace_prompt(ws: Workspace, codes: list[Code]) -> str:
    """The system prompt for a workspace as it stands: what a run would send now."""
    return build_system_prompt(ws.study_context or ws.description or ws.name, codes,
                               getattr(ws, "coding_instructions", None))


def prompt_sha(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def format_context(context_utts: list[dict]) -> str:
    lines = []
    for u in context_utts:
        spk, ts, txt = u.get("speaker", ""), u.get("timestamp", ""), u.get("text", "")
        lines.append(f"{spk} [{ts}]: {txt}".strip() if spk else txt)
    return "\n".join(lines)


def parse_json_response(content: str):
    content = content.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", content)
    if match:
        content = match.group(1).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


def normalize_reply(parsed) -> list | None:
    """The reply as a list of entries, whatever shape it came in: the current
    {"codings": [...]} object, or the bare array older prompts asked for. Empty strings
    in the optional fields become None, so a structured reply reads like a free one."""
    if isinstance(parsed, dict):
        parsed = parsed.get("codings")
    if not isinstance(parsed, list):
        return None
    entries = []
    for e in parsed:
        if not isinstance(e, dict):
            continue
        entries.append({k: (v if not (isinstance(v, str) and not v.strip()) else None)
                        for k, v in e.items()})
    return entries


# ── API calls ─────────────────────────────────────────────────────────────────

# Why a unit could not be coded. The reason is stored as "<code>: <detail>": the code
# is a stable identifier to count and to translate, the detail is raw (a limit, a
# category, a library's own message) and passes through untouched.
ERROR_CODES = ("truncated", "refusal", "no_text", "bad_json", "rate_limited", "api")


def error_reason(code: str, detail: str = "") -> str:
    assert code in ERROR_CODES, code
    return f"{code}: {detail}"[:300] if detail else code


class CallResult(NamedTuple):
    """One API call. `entries` is None when the unit could not be coded, and then
    `error` says why: the reason travels to the unit instead of being swallowed."""
    entries: list | None
    tokens_in: int
    tokens_out: int
    cost: float
    error: str | None = None


def request_config(model: str) -> dict:
    """How a model is called: recorded on the run so the call can be reproduced."""
    p = MODEL_PROFILES.get(model, MODEL_PROFILES["claude-sonnet-4-6"])
    return {"model": model, "max_tokens": p["max_tokens"], "effort": p["effort"],
            "structured_output": p["structured"], "thinking": "default" if p["thinks"] else "off"}


def call_claude(client, system_prompt, text, context_utts=None,
                model="claude-sonnet-4-6") -> CallResult:
    """One unit, one call. Prompt caching on system.

    `tokens_in` counts every input token, cached or not, so the run's token totals keep
    their meaning. The cost cannot be derived from that total: cache writes and reads
    are billed at different rates, so it is priced here, per call, from the breakdown
    the API returns — the only place where that breakdown exists."""
    profile = MODEL_PROFILES.get(model, MODEL_PROFILES["claude-sonnet-4-6"])
    if context_utts:
        user_content = (
            f"[CONTEXT — surrounding utterances, do not code]\n"
            f"{format_context(context_utts)}\n\n"
            f"[EXCERPT TO CODE]\n\"{text.strip()}\""
        )
    else:
        user_content = f'Analyze this excerpt:\n"{text.strip()}"'

    output_config = {}
    if profile["structured"]:
        output_config["format"] = {"type": "json_schema", "schema": CODINGS_SCHEMA}
    if profile["effort"]:
        output_config["effort"] = profile["effort"]
    kwargs = {"output_config": output_config} if output_config else {}

    response = client.messages.create(
        model=model,
        max_tokens=profile["max_tokens"],
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_content}],
        **kwargs,
    )
    usage = response.usage
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    tokens_in = usage.input_tokens + cache_write + cache_read
    cost = calc_cost(model, usage.input_tokens, usage.output_tokens,
                     cache_write_tokens=cache_write, cache_read_tokens=cache_read)

    def failed(reason):
        return CallResult(None, tokens_in, usage.output_tokens, cost, reason)

    # a reply cut at the ceiling, or a declined one, may still parse into something:
    # it is not a coding, and it must not be read as one
    stop = getattr(response, "stop_reason", None)
    if stop == "max_tokens":
        return failed(error_reason("truncated", f"max_tokens={profile['max_tokens']}"))
    if stop == "refusal":
        details = getattr(response, "stop_details", None)
        return failed(error_reason("refusal", getattr(details, "category", None) or ""))
    # thinking models put thinking blocks before the text, so the reply is not content[0]
    text_out = next((b.text for b in response.content if getattr(b, "type", "text") == "text"), None)
    if text_out is None:
        return failed(error_reason("no_text"))
    entries = normalize_reply(parse_json_response(text_out))
    if entries is None:
        return failed(error_reason("bad_json"))
    return CallResult(entries, tokens_in, usage.output_tokens, cost)


def call_claude_with_retry(client, system_prompt, text, context_utts=None,
                           model="claude-sonnet-4-6", max_retries=5) -> CallResult:
    """Exponential backoff on rate limits. Any other failure becomes a CallResult with
    the reason, so the unit shows why it was not coded."""
    for attempt in range(max_retries):
        try:
            return call_claude(client, system_prompt, text, context_utts, model)
        except anthropic.RateLimitError as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return CallResult(None, 0, 0, 0.0, error_reason("rate_limited", str(e)))
        except Exception as e:
            return CallResult(None, 0, 0, 0.0, error_reason("api", f"{type(e).__name__}: {e}"))
    return CallResult(None, 0, 0, 0.0, error_reason("api", "no attempt made"))


# ── Offsets (per_utterance QDPX anchoring) ────────────────────────────────────

def find_utterance_offsets(full_text: str, utterance_texts: list[str]) -> list[tuple[int, int]]:
    """
    Character offsets of each utterance within full_text, with an advancing cursor
    to handle repeated phrases. Ported verbatim from the notebook.
    """
    offsets = []
    cursor = 0
    for utt in utterance_texts:
        idx = full_text.find(utt, cursor)
        if idx < 0:
            idx = full_text.find(utt)
        if idx >= 0:
            offsets.append((idx, idx + len(utt)))
            cursor = idx + len(utt)
        else:
            offsets.append((0, len(utt)))
    return offsets


# ── Run execution ─────────────────────────────────────────────────────────────

def _active_codes(db, workspace_id: int) -> list[Code]:
    return (db.query(Code)
            .filter(Code.workspace_id == workspace_id, Code.is_deleted == False)
            .order_by(Code.label).all())


def _resolve_unit(ws: Workspace, unit: str, source_type: str) -> str:
    """
    Map a run's stored unit to a concrete one. Legacy runs stored per_row /
    per_utterance, whose meaning depended on the workspace settings — resolved
    here the way the old engine did, so historical retries stay consistent.
    """
    if source_type == "excel":
        if unit in ("cell", "sentence"):
            return unit
        return ws.segmentation_mode  # legacy excel runs followed the settings
    if unit == "per_row":
        return "document"
    if unit == "per_utterance":
        return ws.segmentation_mode
    return unit  # document | utterance_regex | paragraph | sentence


def _units_for_document(ws: Workspace, doc, unit: str, context_window: int,
                        excluded_roles: list[str] | None = None) -> tuple[str, list[dict], list]:
    """
    Coding units for one document: (full_text, units, contexts).
    Each unit: {"text", "speaker", "timestamp", "row_index", "excluded"}.

    The unit comes from the run (snapshot of the workspace segmentation at launch).
    Excel: every non-empty cell is a respondent — no cross-row context, ever; in
    sentence mode the full cell is shown as context for its own sentences.
    DOCX utterance mode resolves the document's convention, applies the speaker
    inventory + carry-forward, and marks front matter and excluded-role units —
    excluded units are never sent to the engine but stay in the context window.
    """
    source_type = getattr(doc, "source_type", "docx")
    unit = _resolve_unit(ws, unit, source_type)
    excluded = set(excluded_roles or [])

    def mk(text, speaker=None, timestamp="", row_index=None, excl=False):
        return {"text": text, "speaker": speaker, "timestamp": timestamp,
                "row_index": row_index, "excluded": excl}

    if source_type == "excel":
        cfg = json.loads(doc.source_config or "{}")
        cells = load_excel_cells(doc.file_path, cfg.get("sheet"), cfg["column"],
                                 cfg.get("group_column"), cfg.get("group_value"))
        units, contexts = [], []
        if unit == "sentence":
            lang = doc.language or ws.segmentation_language
            for row_n, cell in cells:
                sentences = split_sentences(cell, lang)
                for s in sentences:
                    units.append(mk(s["text"], row_index=row_n))
                    contexts.append([{"speaker": "", "timestamp": "", "text": cell}]
                                    if len(sentences) > 1 else None)
        else:  # cell
            for row_n, cell in cells:
                units.append(mk(cell, row_index=row_n))
                contexts.append(None)
        return excel_fulltext(cells), units, contexts

    full_text = load_document_text(doc.file_path)
    if unit == "document":
        return full_text, [mk(full_text.strip())], [None]

    lang = doc.language or ws.segmentation_language   # sentence sub-splitting
    doc_conv = conventions.resolve_convention(ws, getattr(doc, "convention", None))
    roles = {}
    try:
        roles = json.loads(doc.roles_json or "{}")
    except Exception:
        pass
    accepted = list(roles.keys()) or None

    def _excl(speaker, front):
        if front:
            return True
        role = roles.get(speaker, "participant") if speaker else None
        return role in excluded if role else False

    # Speaker/role metadata is orthogonal to the sub-unit granularity: when the
    # document has a convention we parse it into utterances first (speaker, role,
    # front matter, carry-forward), then the unit decides the sub-division — so
    # role exclusion works in sentence/paragraph too, not just utterance_regex.
    # Excluded turns stay whole (engine skips them, they remain as context); coded
    # turns get sub-segmented. Without a convention, sentence/paragraph fall back to
    # the speaker-blind split.
    speaker_aware = unit == "utterance_regex" or (doc_conv and unit in ("sentence", "paragraph"))
    if speaker_aware:
        regex = doc_conv or ws.segmentation_regex
        units = []
        for u in split_utterances(full_text, regex, accepted):
            txt = u["text"].strip()
            if not txt:
                continue
            excl = _excl(u["speaker"], u["front"])
            speaker = u["speaker"] or None
            if excl or unit in ("utterance_regex", "paragraph"):
                pieces = [txt]
            else:  # sentence: split the turn, each sentence keeps the speaker
                pieces = [s["text"].strip() for s in split_sentences(txt, lang)
                          if s["text"].strip()]
            for p in pieces:
                units.append(mk(p, speaker=speaker, timestamp=u["timestamp"], excl=excl))
    else:  # paragraph | sentence without a convention: speaker-blind
        units = [mk(s["text"].strip())
                 for s in segment_text(full_text, unit, ws.segmentation_regex, lang)
                 if s["text"].strip()]
    contexts = [units[max(0, i - context_window):i] if context_window > 0 else None
                for i in range(len(units))]
    return full_text, units, contexts


def _resolve_code(db, run: Run, label: str, description: str | None,
                  example: str | None, label_map: dict[str, Code]) -> Code:
    """
    Insert-time dedup: return the existing active code whose normalized label matches,
    otherwise create a model-proposed code and register it in the map.
    """
    norm = normalize_label(label)
    if norm in label_map:
        return label_map[norm]
    code = Code(workspace_id=run.workspace_id, label=label.strip(),
                description=(description or "").strip() or None,
                example=(example or "").strip() or None,
                is_model_proposed=True, proposed_in_run_id=run.id,
                created_by_id=None, updated_by_id=None)
    db.add(code)
    db.flush()
    label_map[norm] = code
    return code


def _process_document(db, run: Run, ws: Workspace, run_doc: RunDocument, client,
                      dict_index: dict | None = None) -> tuple[int, int, float]:
    """Code one document. Returns (tokens_in, tokens_out, cost_usd). Raises on hard failure."""
    # layer 2: rebuild prompt from the codebook as it is *now* (document boundary reload)
    codes = _active_codes(db, ws.id)
    label_map = {normalize_label(c.label): c for c in codes}
    code_by_id = {c.id: c for c in codes}

    doc = run_doc.document
    unit = _resolve_unit(ws, run.granularity, getattr(doc, "source_type", "docx"))
    excluded_roles = []
    try:
        excluded_roles = json.loads(run.excluded_roles_snapshot or "[]")
    except Exception:
        pass
    full_text, units, contexts = _units_for_document(
        ws, doc, run.granularity, run.context_window, excluded_roles)
    if not units:
        raise ValueError("No segments produced — check the segmentation settings")

    # excluded units (front matter, excluded roles) never reach the engine,
    # but they stay in `contexts` so the model still sees e.g. the question
    jobs = [(i, u, contexts[i]) for i, u in enumerate(units) if not u["excluded"]]
    responses = [None] * len(units)
    errors = [None] * len(units)
    tokens_in = tokens_out = 0
    cost = 0.0

    if run.engine == "dictionary":
        language = doc.language or ws.segmentation_language
        if not language:
            raise ValueError("No language set for this document — set it in the Corpus tab")
        for pos, u, _ctx in jobs:
            matches = dictionary.match_unit(u["text"], language, dict_index or {})
            entries = []
            for code_id, m in matches.items():
                if code_id not in code_by_id:
                    continue
                entries.append({
                    "action": "use_existing",
                    "_code_id": code_id,
                    "_matched": m["expressions"],
                    "_score": m["score"],
                    "rationale": (f"Matched expressions: {', '.join(m['expressions'])} — "
                                  f"in: \"{m['sentences'][0]}\""),
                })
            responses[pos] = entries
    else:
        system_prompt = workspace_prompt(ws, codes)
        # audit: the exact prompt this document was coded with, stored once per version
        sha = prompt_sha(system_prompt)
        prompts = json.loads(run.system_prompts_json or "{}")
        if sha not in prompts:
            prompts[sha] = system_prompt
            run.system_prompts_json = json.dumps(prompts, ensure_ascii=False)
        run_doc.system_prompt_sha = sha

        def record(pos, result: CallResult):
            nonlocal tokens_in, tokens_out, cost
            responses[pos] = result.entries  # None = the unit could not be coded
            errors[pos] = result.error
            tokens_in += result.tokens_in
            tokens_out += result.tokens_out
            cost += result.cost

        # A cache entry becomes readable only once the first response has started, so
        # calls fired together all miss and each pays a full write. The first unit
        # goes alone; the rest then read the prompt it cached.
        if jobs:
            pos, u, ctx = jobs[0]
            record(pos, call_claude_with_retry(client, system_prompt, u["text"], ctx, run.model))
        with ThreadPoolExecutor(max_workers=run.max_workers) as executor:
            future_to_pos = {
                executor.submit(call_claude_with_retry, client, system_prompt,
                                u["text"], ctx, run.model): pos
                for pos, u, ctx in jobs[1:]
            }
            for future in as_completed(future_to_pos):
                record(future_to_pos[future], future.result())

    offsets = None
    if unit != "document":
        offsets = find_utterance_offsets(full_text, [u["text"] for u in units])

    for pos, u in enumerate(units):
        start, end = offsets[pos] if offsets else (None, None)
        if u["excluded"]:
            db.add(RunSegment(run_id=run.id, document_id=run_doc.document_id, position=pos,
                              segment_text=u["text"], start_offset=start, end_offset=end,
                              row_index=u["row_index"], speaker=u["speaker"],
                              status="excluded"))
            continue
        response = responses[pos]
        n_coded = 0
        no_code_rationales = []
        for entry in (response or []):
            action = entry.get("action")
            if action == "no_code":
                if (entry.get("rationale") or "").strip():
                    no_code_rationales.append(entry["rationale"].strip())
                continue
            if "_code_id" in entry:  # dictionary engine: code resolved by id, no proposals
                code = code_by_id[entry["_code_id"]]
            else:
                label = (entry.get("code") or "").strip()
                if not label:
                    continue
                # use_existing with an unknown label is treated like a proposal:
                # better a visible model-proposed code than a silently dropped coding
                code = _resolve_code(db, run, label, entry.get("description"),
                                     entry.get("example"), label_map)
            db.add(Coding(run_id=run.id, document_id=run_doc.document_id, code_id=code.id,
                          segment_text=u["text"], start_offset=start,
                          end_offset=end, row_index=u["row_index"], speaker=u["speaker"],
                          rationale=entry.get("rationale"),
                          matched_expressions=(json.dumps(entry["_matched"], ensure_ascii=False)
                                               if "_matched" in entry else None),
                          relevance_score=entry.get("_score")))
            n_coded += 1
        # the complete photograph: one RunSegment per processed unit, coded or not
        status = "error" if response is None else ("coded" if n_coded else "no_code")
        db.add(RunSegment(run_id=run.id, document_id=run_doc.document_id, position=pos,
                          segment_text=u["text"], start_offset=start,
                          end_offset=end, row_index=u["row_index"], speaker=u["speaker"],
                          status=status,
                          no_code_rationale=" | ".join(no_code_rationales) or None,
                          error_reason=errors[pos] if status == "error" else None))
    return tokens_in, tokens_out, cost


def execute_run(run_id: int):
    """
    Background thread entry point.

    Setup and finalize run on a long-lived session; the document loop runs on a
    fresh, short-lived session per document. This is deliberate: a single session
    held for the whole run keeps every Coding and RunSegment it ever adds in its
    identity map (commit expires attributes but does not release the objects), so
    memory grows monotonically with the corpus — on a large corpus that growth is
    what exhausts RAM. Closing the per-document session drops that document's rows
    and keeps the working set flat.
    """
    db = SessionLocal()
    try:
        run = db.query(Run).filter(Run.id == run_id).first()
        if not run:
            return
        ws = db.query(Workspace).filter(Workspace.id == run.workspace_id).first()
        user = db.query(User).filter(User.id == run.created_by_id).first()
        workspace_id = ws.id

        client = None
        dict_index = None
        if run.engine == "dictionary":
            dict_index = dictionary.build_index(db, ws.id)  # plain data, session-independent
        else:
            if not user or not user.api_key_encrypted:
                run.status = "failed"
                run.error_message = "No API key on the user profile"
                db.commit()
                return
            client = anthropic.Anthropic(api_key=decrypt_api_key(user.api_key_encrypted))

        if not run.codebook_snapshot_json:  # audit snapshot, only on first launch
            run.codebook_snapshot_json = json.dumps(
                [{"label": c.label, "description": c.description, "example": c.example}
                 for c in _active_codes(db, ws.id)], ensure_ascii=False)
        if run.engine == "llm" and not run.request_config_json:
            run.request_config_json = json.dumps(request_config(run.model))
        run.status = "running"
        run.started_at = run.started_at or datetime.utcnow()
        run.error_message = None
        pending_doc_ids = [rd.document_id for rd in run.run_documents
                           if rd.status != "completed"]
        db.commit()

        failed_files = []
        for document_id in pending_doc_ids:
            doc_db = SessionLocal()
            try:
                run_ = doc_db.get(Run, run_id)
                ws_ = doc_db.get(Workspace, workspace_id)
                run_doc = doc_db.get(RunDocument, (run_id, document_id))
                try:
                    t_in, t_out, doc_cost = _process_document(doc_db, run_, ws_, run_doc,
                                                              client, dict_index)
                    run_doc.status = "completed"
                    run_doc.coded_at = datetime.utcnow()
                except Exception as e:
                    doc_db.rollback()  # drop this document's partial Coding/RunSegment
                    run_doc = doc_db.get(RunDocument, (run_id, document_id))
                    run_doc.status = "failed"
                    failed_files.append(f"{run_doc.document.filename}: {e}")
                    t_in = t_out = 0
                    doc_cost = 0.0
                run_ = doc_db.get(Run, run_id)
                run_.cost_input_tokens += t_in
                run_.cost_output_tokens += t_out
                # summed per call, not recomputed from the token totals: those totals
                # mix cached and uncached input, which are billed at different rates
                run_.cost_usd = (run_.cost_usd or 0.0) + doc_cost
                doc_db.commit()  # per-document commit: polling sees progress, retry sees state
            finally:
                doc_db.close()  # releases this document's rows from memory

        db.refresh(run)  # cost was accumulated in the per-document sessions
        run.status = "completed"
        run.completed_at = datetime.utcnow()
        if failed_files:  # partial failure is not a blocking error (spec)
            run.error_message = "Failed documents: " + "; ".join(failed_files)

        if user:
            log = (db.query(UserCostLog)
                   .filter(UserCostLog.run_id == run.id, UserCostLog.user_id == user.id).first())
            if not log:
                log = UserCostLog(user_id=user.id, run_id=run.id)
                db.add(log)
            log.input_tokens = run.cost_input_tokens
            log.output_tokens = run.cost_output_tokens
            log.cost_usd = run.cost_usd
            log.recorded_at = datetime.utcnow()
        db.commit()
    except Exception:
        db.rollback()
        run = db.query(Run).filter(Run.id == run_id).first()
        if run:
            run.status = "failed"
            run.error_message = traceback.format_exc(limit=3)
            run.completed_at = datetime.utcnow()
            db.commit()
    finally:
        db.close()


# ── Cost estimate (pre-run, non-blocking) ─────────────────────────────────────

EST_SAMPLE_DOCS = 8  # segment at most this many documents, then extrapolate


def estimate_run_cost(ws: Workspace, documents, unit: str,
                      context_window: int, model: str, codes: list[Code],
                      excluded_roles: list[str] | None = None) -> dict:
    """
    Rough estimate: chars/4 ≈ tokens. The cached system prompt is priced as one cache
    write per document and cache reads for every other call.

    Segmenting the whole corpus here would redo the run's work (spaCy on every
    document for sentence mode) and time out on large corpora — so we segment a
    sample of documents and scale to the full count. It is a "circa" estimate, and
    rougher still on the thinking models, whose output includes thinking we can only
    guess at.
    """
    system_tokens = len(workspace_prompt(ws, codes)) // 4
    docs = list(documents)
    sample = docs[:EST_SAMPLE_DOCS]
    n_seg = seg_tokens = ctx_tokens = counted = 0
    excluded_roles = excluded_roles or []
    for doc in sample:
        try:
            _, units, contexts = _units_for_document(ws, doc, unit, context_window,
                                                     excluded_roles)
        except Exception:
            continue
        counted += 1
        for u, ctx in zip(units, contexts):
            if u["excluded"]:
                continue
            n_seg += 1
            seg_tokens += len(u["text"]) // 4
            if ctx:
                ctx_tokens += sum(len(c["text"]) for c in ctx) // 4

    if counted == 0 or n_seg == 0:
        return {"segments": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    scale = len(docs) / counted  # extrapolate the sample to the whole corpus
    n_segments = round(n_seg * scale)
    seg_tokens_total = round(seg_tokens * scale)
    ctx_tokens_total = round(ctx_tokens * scale)

    # one cache write per document (the first unit goes alone), reads for the rest
    n_docs = len(docs)
    cache_write = system_tokens * min(n_docs, n_segments)
    cache_read = system_tokens * max(0, n_segments - n_docs)
    uncached = seg_tokens_total + ctx_tokens_total + EST_PROMPT_OVERHEAD_TOKENS * n_segments
    per_unit_out = EST_OUTPUT_TOKENS_PER_SEGMENT
    if MODEL_PROFILES.get(model, {}).get("thinks"):
        per_unit_out += EST_THINKING_TOKENS_PER_SEGMENT
    output_tokens = per_unit_out * n_segments
    return {
        "segments": n_segments,
        "input_tokens": uncached + cache_write + cache_read,
        "output_tokens": output_tokens,
        "cost_usd": round(calc_cost(model, uncached, output_tokens,
                                    cache_write_tokens=cache_write,
                                    cache_read_tokens=cache_read), 4),
    }
