"""
Export generators for Autocode web app — ported from autocode.ipynb cells 10/11.

All functions return bytes (served as downloads, nothing written to disk):
  export_xlsx_bytes — flat codings table + codebook sheet (always available)
  export_qdc_bytes  — REFI-QDA codebook, ISO 24277 (always available)
  export_qdpx_bytes — REFI-QDA project, MAXQDA-compatible (only if run.qdpx_enabled)

MAXQDA conventions (reverse-engineered, see wiki autocode.md):
- plainTextPath uses 'internal://<guid>.txt'
- source files in 'sources/' (lowercase) named by guid
- codings wrapped in <PlainTextSelection startPosition endPosition>
- modifyingUser/modifiedDateTime on all elements, xsi:schemaLocation on <Project>
"""
import io
import re
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime

import pandas as pd

import json

from models import Code, CodeExpression, Coding, Run, RunSegment
from segmentation import SPACY_MODELS, document_fulltext


# ── Excel ─────────────────────────────────────────────────────────────────────

def export_xlsx_bytes(run: Run, db) -> bytes:
    codings = (db.query(Coding).filter(Coding.run_id == run.id)
               .order_by(Coding.document_id, Coding.start_offset).all())
    rows = [{
        "document": c.document.display_name,
        "group": c.document.group_label or "",
        "excel_row": c.row_index,
        "speaker": c.speaker or "",
        "segment_text": c.segment_text,
        "code": c.code.label,
        "rationale": c.rationale or "",
        "matched_expressions": ("; ".join(json.loads(c.matched_expressions))
                                if c.matched_expressions else ""),
        "relevance_score": c.relevance_score,
        "start_offset": c.start_offset,
        "end_offset": c.end_offset,
        "model_proposed_code": c.code.is_model_proposed,
    } for c in codings]
    codes = (db.query(Code)
             .filter(Code.workspace_id == run.workspace_id, Code.is_deleted == False)
             .order_by(Code.label).all())
    expr_map: dict[tuple, list[str]] = {}
    for e in (db.query(CodeExpression)
              .join(Code, Code.id == CodeExpression.code_id)
              .filter(Code.workspace_id == run.workspace_id).all()):
        expr_map.setdefault((e.code_id, e.language), []).append(e.expression)
    cb_rows = [{
        "Code": c.label,
        "Description": c.description or "",
        "Example": c.example or "",
        "Proposed by model": c.is_model_proposed,
        **{f"Expressions_{lang}": "; ".join(expr_map.get((c.id, lang), []))
           for lang in sorted(SPACY_MODELS)},
    } for c in codes]

    # full coverage: every processed segment, coded or not, with its codes joined
    labels_by_key: dict[tuple, list[str]] = {}
    for c in codings:
        key = (c.document_id, c.start_offset, c.end_offset, c.segment_text)
        labels_by_key.setdefault(key, []).append(c.code.label)
    segments = (db.query(RunSegment).filter(RunSegment.run_id == run.id)
                .order_by(RunSegment.document_id, RunSegment.position).all())
    seg_rows = [{
        "document": s.document.display_name,
        "excel_row": s.row_index,
        "speaker": s.speaker or "",
        "position": s.position,
        "segment_text": s.segment_text,
        "status": s.status,
        "codes": "; ".join(labels_by_key.get(
            (s.document_id, s.start_offset, s.end_offset, s.segment_text), [])),
        "no_code_rationale": s.no_code_rationale or "",
    } for s in segments]

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, sheet_name=f"run_{run.id}_codings", index=False)
        if seg_rows:  # legacy runs predate RunSegment and have no coverage data
            pd.DataFrame(seg_rows).to_excel(writer, sheet_name="segments", index=False)
        pd.DataFrame(cb_rows).to_excel(writer, sheet_name="codebook", index=False)
    return buf.getvalue()


def export_codebook_bytes(workspace_id: int, db) -> bytes:
    """Current active codebook as Excel, import-compatible (round trip)."""
    codes = (db.query(Code)
             .filter(Code.workspace_id == workspace_id, Code.is_deleted == False)
             .order_by(Code.label).all())
    expr_map: dict[tuple, list[str]] = {}
    for e in (db.query(CodeExpression)
              .join(Code, Code.id == CodeExpression.code_id)
              .filter(Code.workspace_id == workspace_id).all()):
        expr_map.setdefault((e.code_id, e.language), []).append(e.expression)
    rows = [{
        "Code": c.label,
        "Description": c.description or "",
        "Example": c.example or "",
        **{f"Expressions_{lang}": "; ".join(expr_map.get((c.id, lang), []))
           for lang in sorted(SPACY_MODELS)},
    } for c in codes]
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, sheet_name="codebook", index=False)
    return buf.getvalue()


def export_analysis_bytes(data: dict) -> bytes:
    """All the analysis blocks as one workbook, one sheet per block."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(data["codes"]).drop(columns=["code_id"], errors="ignore") \
            .to_excel(writer, sheet_name="codes", index=False)

        if data.get("groups"):
            g = data["groups"]
            rows = []
            for r in g["rows"]:
                for grp in g["groups"]:
                    rows.append({"code": r["label"], "group": grp or "(no group)",
                                 "pct_units_coded": r["pct"][grp],
                                 "deviation_from_mean": r["deviation"][grp],
                                 "cross_group_mean": r["mean"]})
            pd.DataFrame(rows).to_excel(writer, sheet_name="groups", index=False)

        c = data["cooccurrence"]
        if c["labels"]:
            pd.DataFrame(c["matrix"], index=c["labels"], columns=c["labels"]) \
                .to_excel(writer, sheet_name="cooccurrence")

        d = data["documents"]
        if d["rows"]:
            pd.DataFrame([{
                "document": r["document"], "group": r["group"],
                "segments": r["segments"], "coded": r["coded"],
                "coverage_pct": r["coverage_pct"],
                **dict(zip(d["codes"], r["counts"])),
            } for r in d["rows"]]).to_excel(writer, sheet_name="documents", index=False)

        for lang, block in sorted(data.get("lemmas", {}).items()):
            pd.DataFrame(block.get("all", block["top"])).to_excel(
                writer, sheet_name=f"lemmas_{lang}", index=False)

        for lang, cells in sorted(data.get("lemmas_by_code", {}).items()):
            rows = [{"code": c["code"], "lemma": x["lemma"], "freq": x["freq"],
                     "norm": x["norm"], "total_lemmas": c["total"],
                     "low_volume": c["flagged"]}
                    for c in cells for x in c.get("all", c["top"])]
            if rows:
                pd.DataFrame(rows).to_excel(writer, sheet_name=f"lemmas_by_code_{lang}",
                                            index=False)
        for lang, cells in sorted(data.get("lemmas_by_code_group", {}).items()):
            rows = [{"code": c["code"], "group": c["group"] or "(no group)",
                     "lemma": x["lemma"], "freq": x["freq"], "norm": x["norm"],
                     "total_lemmas": c["total"], "low_volume": c["flagged"]}
                    for c in cells for x in c.get("all", c["top"])]
            if rows:
                pd.DataFrame(rows).to_excel(writer,
                                            sheet_name=f"lemmas_by_code_group_{lang}",
                                            index=False)

        # expression firings: overall per language + per-(code, group) long format
        for lang, rows in sorted((data.get("expressions") or {}).items()):
            if rows:
                pd.DataFrame(rows).to_excel(writer, sheet_name=f"expressions_{lang}",
                                            index=False)
        for lang, cells in sorted((data.get("expressions_by_code_group") or {}).items()):
            rows = [{"code": x["code"], "group": x["group"] or "(no group)",
                     "expression": x["expression"], "firings": x["firings"]}
                    for x in cells]
            if rows:
                pd.DataFrame(rows).to_excel(writer,
                                            sheet_name=f"expr_by_code_group_{lang}",
                                            index=False)

        # top extracts with the group column (option B: ranked per code × group)
        by_group = data.get("top_extracts_by_group")
        if by_group:
            rows = [{"code": blk["code"], "group": blk["group"] or "(no group)",
                     "score": e["score"], "document": e["document"],
                     "excel_row": e["row"], "text": e["text"]}
                    for blk in by_group for e in blk["extracts"]]
            if rows:
                pd.DataFrame(rows).to_excel(writer, sheet_name="top_extracts", index=False)
        elif data.get("top_extracts"):  # analyses cached before the group breakdown
            rows = [{"code": blk["code"], "score": e["score"], "document": e["document"],
                     "excel_row": e["row"], "text": e["text"]}
                    for blk in data["top_extracts"] for e in blk["extracts"]]
            if rows:
                pd.DataFrame(rows).to_excel(writer, sheet_name="top_extracts", index=False)
    return buf.getvalue()


# ── QDC (REFI-QDA codebook) ───────────────────────────────────────────────────

def export_qdc_bytes(codes: list[Code]) -> bytes:
    root = ET.Element("CodeBook", {"origin": "Autocode", "xmlns": "urn:QDA-XML:codebook:1:0"})
    codes_elem = ET.SubElement(root, "Codes")
    for code in codes:
        code_elem = ET.SubElement(codes_elem, "Code", {
            "guid": str(uuid.uuid4()),
            "name": code.label,
            "isCodable": "true",
        })
        if (code.description or "").strip():
            ET.SubElement(code_elem, "Description").text = code.description.strip()
    ET.indent(root, space="  ")
    buf = io.BytesIO()
    ET.ElementTree(root).write(buf, encoding="utf-8", xml_declaration=True)
    return buf.getvalue()


# ── QDPX (REFI-QDA project) ───────────────────────────────────────────────────

def export_qdpx_bytes(run: Run, db) -> bytes:
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    user_guid = str(uuid.uuid4()).upper()

    # codebook: active workspace codes plus anything referenced by this run's codings
    # (soft-deleted codes included, so historical codings stay anchored)
    active = (db.query(Code)
              .filter(Code.workspace_id == run.workspace_id, Code.is_deleted == False).all())
    referenced_ids = {c.code_id for c in db.query(Coding).filter(Coding.run_id == run.id)}
    extra = (db.query(Code).filter(Code.id.in_(referenced_ids)).all()
             if referenced_ids else [])
    all_codes = {c.id: c for c in active}
    for c in extra:
        all_codes.setdefault(c.id, c)
    code_guid_map = {cid: str(uuid.uuid4()).upper() for cid in all_codes}

    NS = "urn:QDA-XML:project:1.0"
    XSI = "http://www.w3.org/2001/XMLSchema-instance"
    ET.register_namespace("", NS)
    ET.register_namespace("xsi", XSI)
    tag = lambda t: f"{{{NS}}}{t}"

    root = ET.Element(tag("Project"), {
        "origin": "Autocode",
        "name": f"Autocode run {run.id} — {run.workspace.name}",
        f"{{{XSI}}}schemaLocation": (
            "urn:QDA-XML:project:1.0 "
            "http://schema.qdasoftware.org/versions/Project/v1.0/Project.xsd"
        ),
        "modifiedDateTime": now_iso,
    })

    ET.SubElement(ET.SubElement(root, tag("Users")), tag("User"),
                  {"guid": user_guid, "name": "Autocode"})

    codes_elem = ET.SubElement(ET.SubElement(root, tag("CodeBook")), tag("Codes"))
    for cid, code in all_codes.items():
        ce = ET.SubElement(codes_elem, tag("Code"), {
            "guid": code_guid_map[cid], "name": code.label,
            "isCodable": "true", "color": "#0000a5",
        })
        if (code.description or "").strip():
            ET.SubElement(ce, tag("Description")).text = code.description.strip()

    sources_elem = ET.SubElement(root, tag("Sources"))
    source_files = {}  # archive path → bytes

    def common_attrs():
        return {
            "creatingUser": user_guid,
            "creationDateTime": now_iso,
            "modifyingUser": user_guid,
            "modifiedDateTime": now_iso,
        }

    completed_doc_ids = [rd.document_id for rd in run.run_documents if rd.status == "completed"]
    codings_by_doc: dict[int, list[Coding]] = {}
    for c in (db.query(Coding).filter(Coding.run_id == run.id)
              .order_by(Coding.start_offset).all()):
        codings_by_doc.setdefault(c.document_id, []).append(c)

    for rd in run.run_documents:
        if rd.document_id not in completed_doc_ids:
            continue
        doc = rd.document
        try:
            full_text = document_fulltext(doc)
        except Exception:
            continue  # source file gone: skip the document, keep the rest of the export
        if not full_text.strip():
            continue

        src_guid = str(uuid.uuid4()).upper()
        file_name = f"{src_guid}.txt"
        source_files[f"sources/{file_name}"] = full_text.encode("utf-8")
        display_name = re.sub(r"\.(docx|txt|pdf|xlsx)\b", "", doc.display_name, flags=re.I)
        ts = ET.SubElement(sources_elem, tag("TextSource"), {
            "guid": src_guid,
            "name": display_name,
            "plainTextPath": f"internal://{file_name}",
            **common_attrs(),
        })

        # Re-anchor every coding by searching its segment text in the rebuilt source.
        # Stored offsets are only a fallback: they may have been computed against a
        # different fulltext build (e.g. before the [R..] row markers), and a QDPX
        # with drifted selections is worse than useless in MAXQDA.
        cursor = 0
        last_seg: str | None = None
        last_anchor: tuple[int, int] | None = None
        for coding in codings_by_doc.get(doc.id, []):
            seg = coding.segment_text
            if seg == last_seg and last_anchor:
                start, end = last_anchor  # same segment, different code: same selection
            else:
                idx = full_text.find(seg, cursor)
                if idx < 0:
                    idx = full_text.find(seg)
                if idx >= 0:
                    start, end = idx, idx + len(seg)
                    cursor = end
                elif coding.start_offset is not None:
                    start, end = coding.start_offset, coding.end_offset
                else:
                    start, end = 0, len(full_text)
                last_seg, last_anchor = seg, (start, end)
            sel = ET.SubElement(ts, tag("PlainTextSelection"), {
                "guid": str(uuid.uuid4()).upper(),
                "name": f"{start},{end}",
                "startPosition": str(start),
                "endPosition": str(end),
                **common_attrs(),
            })
            coding_elem = ET.SubElement(sel, tag("Coding"), {
                "guid": str(uuid.uuid4()).upper(),
                **common_attrs(),
            })
            ET.SubElement(coding_elem, tag("CodeRef"),
                          {"targetGUID": code_guid_map[coding.code_id]})

    ET.indent(root, space=" ")
    xml_buf = io.BytesIO()
    ET.ElementTree(root).write(xml_buf, encoding="utf-8", xml_declaration=True)

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("project.qde", xml_buf.getvalue())
        for path, content in source_files.items():
            zf.writestr(path, content)
    return zip_buf.getvalue()


# ── Corpus bundle (portable export/import) ────────────────────────────────────

CORPUS_FORMAT = "autocode-corpus"
CORPUS_VERSION = 1


def export_corpus_bytes(ws, db) -> bytes:
    """
    Self-contained corpus bundle: a zip with manifest.json (per-document metadata —
    language, group, convention, roles, source_config — plus the workspace's custom
    convention library) and files/ (the original documents, deduplicated since excel
    column-documents share one workbook). Round-trips into any workspace of the same
    input_type. Runs and codebook are deliberately excluded (corpus only).
    """
    from pathlib import Path
    buf = io.BytesIO()
    file_key: dict[str, str] = {}      # physical path -> manifest key
    file_arcs: dict[str, str] = {}     # key -> arcname inside the zip
    documents = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for doc in ws.documents:
            fp = doc.file_path
            key = file_key.get(fp)
            if key is None and fp and Path(fp).exists():
                key = f"f{len(file_key)}"
                file_key[fp] = key
                arc = f"files/{key}{Path(fp).suffix}"
                file_arcs[key] = arc
                z.write(fp, arc)
            documents.append({
                "file": key,
                "filename": doc.filename,
                "source_type": doc.source_type,
                "source_config": doc.source_config,
                "language": doc.language,
                "group_label": doc.group_label,
                "convention": doc.convention,
                "roles_json": doc.roles_json,
            })
        manifest = {
            "format": CORPUS_FORMAT, "version": CORPUS_VERSION,
            "input_type": ws.input_type,
            "conventions": json.loads(ws.conventions_json or "{}"),
            "files": file_arcs,
            "documents": documents,
        }
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return buf.getvalue()
