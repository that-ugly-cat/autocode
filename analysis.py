"""
Run analysis — the FINK NLP-v8 downstream analyses, computed per run.

Engine-agnostic blocks (LLM and dictionary runs):
  codes         — codings and distinct units per code, % of units covered
  groups        — per group: % of the group's units coded with each code,
                  with deviation from the cross-group mean (coverage-aware
                  normalization via RunSegment, which FINK did not have)
  cooccurrence  — code × code matrix: units carrying both codes
  documents     — document × code counts plus per-document coverage
  lemmas        — per language: lemma frequencies (raw + normalized) over the
                  coded segments, filtered by spaCy stops, punctuation, digits
                  and the workspace's per-language custom stoplist

Dictionary-only blocks:
  expressions   — which expression fired how often, per code
  top_extracts  — top segments per code by relevance score (weighted coding)

The result is cached on Run.analysis_json: a completed run is immutable, so the
cache only needs invalidation when the stoplists change (the Recompute button).
"""
import json
from collections import Counter
from datetime import datetime

import dictionary
from models import Code, Coding, Run, RunSegment, Workspace
from segmentation import SPACY_MODELS

TOP_LEMMAS = 30
TOP_EXTRACTS = 5
MIN_LEMMA_MASS = 50  # cells below this total are flagged as low-volume, not hidden

# Bumped whenever the analysis dict shape changes, so a cached analysis_json from an
# older deploy is recomputed instead of crashing the readers (charts/exports/template).
# 2: expressions split per language + per-(code, group) drill-down; top_extracts_by_group.
ANALYSIS_SCHEMA = 2


def _stoplists(ws: Workspace) -> dict:
    try:
        raw = json.loads(ws.stoplists_json or "{}")
        return {lang: {s.strip().lower() for s in terms if s.strip()}
                for lang, terms in raw.items()}
    except Exception:
        return {}


def compute_analysis(run: Run, ws: Workspace, db, progress: dict | None = None) -> dict:
    """`progress`, if given, is a live dict updated during the lemma loop (the
    dominant cost): {'total': n_coded, 'done': k} — read by the progress endpoint."""
    codings = (db.query(Coding).filter(Coding.run_id == run.id)
               .order_by(Coding.document_id, Coding.start_offset).all())
    segments = (db.query(RunSegment).filter(RunSegment.run_id == run.id)
                .order_by(RunSegment.document_id, RunSegment.position).all())
    code_labels = {c.id: c.label for c in
                   db.query(Code).filter(Code.workspace_id == ws.id).all()}

    n_segments = len(segments)
    n_excluded = sum(1 for s in segments if s.status == "excluded")
    n_coded = sum(1 for s in segments if s.status == "coded")
    # excluded units (front matter, excluded roles) never were codable:
    # they must not inflate any denominator
    codable = [s for s in segments if s.status != "excluded"]
    n_codable = len(codable)

    def unit_key(obj):
        return (obj.document_id, obj.start_offset, obj.end_offset, obj.segment_text)

    units: dict = {}  # unit_key -> set of code_ids
    for c in codings:
        units.setdefault(unit_key(c), set()).add(c.code_id)

    # ── codes ─────────────────────────────────────────────────────────────────
    per_code_codings = Counter(c.code_id for c in codings)
    per_code_units = Counter()
    for code_set in units.values():
        for cid in code_set:
            per_code_units[cid] += 1
    codes_block = sorted(({
        "code_id": cid,
        "label": code_labels.get(cid, f"code {cid}"),
        "codings": per_code_codings[cid],
        "units": per_code_units[cid],
        "pct_units": round(100 * per_code_units[cid] / n_codable, 1) if n_codable else 0,
    } for cid in per_code_codings), key=lambda x: -x["codings"])
    code_order = [b["code_id"] for b in codes_block]

    # ── groups ────────────────────────────────────────────────────────────────
    doc_group = {}
    doc_lang = {}
    doc_name = {}
    for s in segments:
        doc_group[s.document_id] = s.document.group_label or ""
        doc_lang[s.document_id] = s.document.language
        doc_name[s.document_id] = s.document.display_name
    group_names = sorted({g for g in doc_group.values()})
    segs_per_group = Counter(doc_group[s.document_id] for s in codable)
    groups_block = None
    if len(group_names) >= 2:
        per_code_group_units: dict = {}
        for key, code_set in units.items():
            g = doc_group.get(key[0], "")
            for cid in code_set:
                per_code_group_units.setdefault(cid, Counter())[g] += 1
        rows = []
        for cid in code_order:
            pcts = {}
            for g in group_names:
                n = per_code_group_units.get(cid, Counter()).get(g, 0)
                pcts[g] = round(100 * n / segs_per_group[g], 1) if segs_per_group[g] else 0
            mean = round(sum(pcts.values()) / len(group_names), 1)
            rows.append({
                "label": code_labels.get(cid, f"code {cid}"),
                "pct": pcts,
                "mean": mean,
                "deviation": {g: round(pcts[g] - mean, 1) for g in group_names},
            })
        groups_block = {"groups": group_names,
                        "units_per_group": dict(segs_per_group), "rows": rows}

    # ── cooccurrence ──────────────────────────────────────────────────────────
    co = Counter()
    for code_set in units.values():
        ordered = sorted(code_set)
        for i in range(len(ordered)):
            for j in range(i, len(ordered)):
                co[(ordered[i], ordered[j])] += 1
    labels = [code_labels.get(cid, f"code {cid}") for cid in code_order]
    matrix = [[co.get((min(a, b), max(a, b)), 0) for b in code_order] for a in code_order]
    cooccurrence_block = {"labels": labels, "matrix": matrix}

    # ── documents ─────────────────────────────────────────────────────────────
    segs_per_doc = Counter(s.document_id for s in codable)
    coded_per_doc = Counter(s.document_id for s in codable if s.status == "coded")
    per_doc_code_units: dict = {}
    for key, code_set in units.items():
        for cid in code_set:
            per_doc_code_units.setdefault(key[0], Counter())[cid] += 1
    documents_block = {
        "codes": labels,
        "rows": [{
            "document": doc_name[did],
            "group": doc_group.get(did, ""),
            "segments": segs_per_doc[did],
            "coded": coded_per_doc[did],
            "coverage_pct": round(100 * coded_per_doc[did] / segs_per_doc[did], 1)
                            if segs_per_doc[did] else 0,
            "counts": [per_doc_code_units.get(did, Counter()).get(cid, 0)
                       for cid in code_order],
        } for did in sorted(segs_per_doc, key=lambda d: doc_name[d])],
    }

    # ── lemmas (three levels: overall, per code, per code+group; per language) ─
    # One spaCy pass per coded segment; its lemmas are attributed to the overall
    # counter, to every code of the unit (multi-coded segments count in each code,
    # so per-code sums exceed the overall total — by design), and to (code, group).
    stoplists = _stoplists(ws)
    overall: dict = {}                 # lang -> Counter
    by_code: dict = {}                 # (lang, code_label) -> Counter
    by_code_group: dict = {}           # (lang, code_label, group) -> Counter
    if progress is not None:
        progress["total"] = n_coded
        progress["done"] = 0
    for s in segments:
        if s.status != "coded":
            continue
        if progress is not None:
            progress["done"] += 1
        lang = doc_lang.get(s.document_id)
        if lang not in SPACY_MODELS:
            continue
        nlp = dictionary._get_nlp(lang)
        stop = stoplists.get(lang, set())
        lemmas = []
        for t in nlp(s.segment_text):
            if t.is_stop or t.is_punct or getattr(t, "is_digit", False):
                continue
            lemma = t.lemma_.lower().strip()
            if lemma and lemma not in stop:
                lemmas.append(lemma)
        if not lemmas:
            continue
        overall.setdefault(lang, Counter()).update(lemmas)
        group = doc_group.get(s.document_id, "")
        for cid in units.get(unit_key(s), ()):
            label = code_labels.get(cid, f"code {cid}")
            by_code.setdefault((lang, label), Counter()).update(lemmas)
            by_code_group.setdefault((lang, label, group), Counter()).update(lemmas)

    def _cell(counts: Counter) -> dict:
        total = sum(counts.values())
        all_lemmas = [{"lemma": l, "freq": f,
                       "norm": round(f / total, 4) if total else 0}
                      for l, f in counts.most_common()]
        return {
            "total": total,
            "flagged": total < MIN_LEMMA_MASS,
            "top": all_lemmas[:TOP_LEMMAS],
            "all": all_lemmas,
        }

    lemmas_block = {lang: _cell(c) for lang, c in sorted(overall.items())}
    lemmas_by_code_block = {}
    for (lang, label), c in sorted(by_code.items()):
        lemmas_by_code_block.setdefault(lang, []).append({"code": label, **_cell(c)})
    lemmas_by_code_group_block = {}
    for (lang, label, group), c in sorted(by_code_group.items()):
        lemmas_by_code_group_block.setdefault(lang, []).append(
            {"code": label, "group": group, **_cell(c)})

    # ── dictionary-only blocks ────────────────────────────────────────────────
    # expressions: firings split per language, with a per-(code, group) breakdown
    # for the drill-down (mirrors the lemma levels). top extracts: top-N per code
    # AND per (code, group) so a group filter ranks honestly within the group.
    expressions_block = None
    expressions_by_code_group_block = None
    top_extracts_block = None
    top_extracts_by_group_block = None
    if run.engine == "dictionary":
        expr_overall: dict = {}     # lang -> Counter[(code, expr)]
        expr_by_group: dict = {}    # lang -> Counter[(code, group, expr)]
        for c in codings:
            if not c.matched_expressions:
                continue
            lang = doc_lang.get(c.document_id)
            if lang not in SPACY_MODELS:
                continue
            code = code_labels.get(c.code_id, f"code {c.code_id}")
            group = doc_group.get(c.document_id, "")
            for expr in json.loads(c.matched_expressions):
                expr_overall.setdefault(lang, Counter())[(code, expr)] += 1
                expr_by_group.setdefault(lang, Counter())[(code, group, expr)] += 1
        expressions_block = {
            lang: [{"code": code, "expression": expr, "firings": n}
                   for (code, expr), n in counter.most_common()]
            for lang, counter in sorted(expr_overall.items())}
        expressions_by_code_group_block = {  # group stored raw ("") like the lemma blocks
            lang: [{"code": code, "group": group, "expression": expr, "firings": n}
                   for (code, group, expr), n in counter.most_common()]
            for lang, counter in sorted(expr_by_group.items())}

        by_code_codings: dict = {}            # code_id -> [coding]
        by_code_group_codings: dict = {}      # (code_id, group) -> [coding]
        for c in codings:
            by_code_codings.setdefault(c.code_id, []).append(c)
            by_code_group_codings.setdefault(
                (c.code_id, doc_group.get(c.document_id, "")), []).append(c)

        def _extract(c):
            return {"text": c.segment_text, "score": c.relevance_score or 0,
                    "document": doc_name.get(c.document_id, ""), "row": c.row_index}

        def _top(cs):
            return [_extract(c) for c in
                    sorted(cs, key=lambda c: -(c.relevance_score or 0))[:TOP_EXTRACTS]]

        top_extracts_block = [
            {"code": code_labels.get(cid, f"code {cid}"),
             "extracts": _top(by_code_codings.get(cid, []))}
            for cid in code_order]
        top_extracts_by_group_block = [  # group stored raw ("") — display converts
            {"code": code_labels.get(cid, f"code {cid}"),
             "group": g, "extracts": _top(cs)}
            for (cid, g), cs in by_code_group_codings.items()]

    return {
        "schema_version": ANALYSIS_SCHEMA,
        "meta": {
            "run_id": run.id, "engine": run.engine, "unit": run.granularity,
            "n_segments": n_segments, "n_coded": n_coded, "n_excluded": n_excluded,
            "computed_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        },
        "codes": codes_block,
        "groups": groups_block,
        "cooccurrence": cooccurrence_block,
        "documents": documents_block,
        "lemmas": lemmas_block,
        "lemmas_by_code": lemmas_by_code_block,
        "lemmas_by_code_group": lemmas_by_code_group_block,
        "expressions": expressions_block,
        "expressions_by_code_group": expressions_by_code_group_block,
        "top_extracts": top_extracts_block,
        "top_extracts_by_group": top_extracts_by_group_block,
    }


def is_current(run: Run) -> bool:
    """Whether the cached analysis_json matches the current schema (else stale)."""
    if not run.analysis_json:
        return False
    try:
        return json.loads(run.analysis_json).get("schema_version") == ANALYSIS_SCHEMA
    except Exception:
        return False


def get_analysis(run: Run, ws: Workspace, db, recompute: bool = False,
                 progress: dict | None = None) -> dict:
    if not recompute and is_current(run):
        try:
            return json.loads(run.analysis_json)
        except Exception:
            pass
    result = compute_analysis(run, ws, db, progress=progress)
    run.analysis_json = json.dumps(result, ensure_ascii=False)
    db.commit()
    return result
