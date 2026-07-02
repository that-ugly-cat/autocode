"""
Segmentation engine for Autocode web app.

Three workspace-level modes (spec: autocode-webapp.md):
  utterance_regex — line-based split on a configurable pattern (default SPEAKER [HH:MM:SS]: text);
                    ported from autocode.ipynb split_into_utterances()
  paragraph       — each non-empty DOCX paragraph is one unit
  sentence        — spaCy sentence tokenization, model chosen by workspace language

spaCy models are loaded lazily and cached per language: nothing is loaded until the
first sentence-mode segmentation (or preview) actually runs.
"""
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd
from docx import Document as DocxDocument

import conventions
from models import DEFAULT_UTTERANCE_REGEX

SPACY_MODELS = {
    "en": "en_core_web_md",
    "de": "de_core_news_md",
    "fr": "fr_core_news_md",
    "it": "it_core_news_md",
}

_nlp_cache: dict[str, object] = {}


def _get_nlp(language: str):
    if language not in SPACY_MODELS:
        raise ValueError(f"Unsupported language: {language}. Use one of {sorted(SPACY_MODELS)}")
    if language not in _nlp_cache:
        import spacy
        # We only need lemmas and sentence boundaries. ner is useless here, and the
        # parser is dead weight: it is the heaviest component by far, and four md
        # models with it loaded blow past RAM on a small VPS (OOM-kill of the run).
        # exclude (not disable) so it is never even loaded; the lemmatizer MUST stay
        # enabled — the dictionary engine and the expression preview live on
        # token.lemma_. Sentence boundaries come from senter (statistical, shares
        # tok2vec, near-parser quality) — enabled here since it ships disabled when a
        # parser is present — with a rule-based sentencizer as fallback.
        nlp = spacy.load(SPACY_MODELS[language], exclude=["ner", "parser"])
        if "senter" in nlp.disabled:
            nlp.enable_pipe("senter")
        elif not nlp.has_pipe("senter") and not nlp.has_pipe("sentencizer"):
            nlp.add_pipe("sentencizer", first=True)
        _nlp_cache[language] = nlp
    return _nlp_cache[language]


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_docx_text(path: str | Path) -> str:
    doc = DocxDocument(str(path))
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    return "\n".join(paragraphs)


class _ParagraphHTMLParser(HTMLParser):
    """Minimal extractor for noScribe HTML: one line per <p>, <br> as newline."""
    def __init__(self):
        super().__init__()
        self.blocks: list[str] = []
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self._buf.append("\n")

    def handle_endtag(self, tag):
        if tag == "p":
            self._flush()

    def handle_data(self, data):
        self._buf.append(data)

    def _flush(self):
        text = "".join(self._buf)
        for line in text.split("\n"):
            if line.strip():
                self.blocks.append(line.strip())
        self._buf = []

    def result(self) -> str:
        self._flush()
        return "\n".join(self.blocks)


def load_text_file(path: str | Path) -> str:
    raw = Path(path).read_bytes()
    for enc in ("utf-8", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return "\n".join(l.strip() for l in text.splitlines() if l.strip())


def load_html_text(path: str | Path) -> str:
    parser = _ParagraphHTMLParser()
    parser.feed(load_text_file(path))
    return parser.result()


def load_document_text(path: str | Path) -> str:
    """Plain text of a transcript file, by extension (.docx / .txt / .html)."""
    suffix = Path(path).suffix.lower()
    if suffix in (".html", ".htm"):
        return load_html_text(path)
    if suffix == ".txt":
        return load_text_file(path)
    return load_docx_text(path)


def load_excel_cells(path: str | Path, sheet: str | None, column: str,
                     group_column: str | None = None,
                     group_value: str | None = None) -> list[tuple[int, str]]:
    """
    Non-empty cells of one column. Returns (spreadsheet_row, text) pairs where
    spreadsheet_row is the 1-based Excel row number (header = row 1), so
    researchers can trace a coding back to the respondent in the original file.

    If group_column/group_value are given (per-respondent survey condition), only
    the rows whose group_column equals group_value are returned — this is how a
    text column is split into one document per group at import.
    """
    df = pd.read_excel(path, sheet_name=sheet if sheet else 0)
    if column not in df.columns:
        raise ValueError(f"Column not found: {column}")
    gcol = df[group_column] if (group_column and group_column in df.columns) else None
    cells = []
    for idx, value in df[column].items():
        if gcol is not None:
            gv = gcol.get(idx)
            gv = "" if pd.isna(gv) else str(gv).strip()
            if gv != (group_value or ""):
                continue
        text = "" if pd.isna(value) else str(value).strip()
        if text:
            cells.append((int(idx) + 2, text))  # +2: header row + 1-based
    return cells


def excel_fulltext(cells: list[tuple[int, str]]) -> str:
    """
    Canonical concatenation used for both offset anchoring and QDPX sources.
    Each cell is prefixed with its spreadsheet row marker ([R5]) so QDA tools show
    which respondent a passage belongs to; offsets are found by searching the raw
    cell text, so selections never include the marker.
    """
    return "\n".join(f"[R{row}] {text}" for row, text in cells)


def document_fulltext(doc) -> str:
    """Plain text of a Document, dispatching on source_type. Single source of truth:
    coding-time offsets and QDPX export must build the same string."""
    if getattr(doc, "source_type", "docx") == "excel":
        cfg = json.loads(doc.source_config or "{}")
        return excel_fulltext(load_excel_cells(
            doc.file_path, cfg.get("sheet"), cfg["column"],
            cfg.get("group_column"), cfg.get("group_value")))
    return load_document_text(doc.file_path)


def inspect_excel(path: str | Path) -> dict:
    """Sheets, columns and sample values for the upload column picker."""
    sheets = pd.read_excel(path, sheet_name=None)
    out = {}
    for name, df in sheets.items():
        cols = []
        for col in df.columns:
            series = df[col].dropna().astype(str).str.strip()
            series = series[series != ""]
            cols.append({
                "name": str(col),
                "n_values": int(series.shape[0]),
                "samples": series.head(3).tolist(),
            })
        out[name] = cols
    return out


# ── Segmenters ────────────────────────────────────────────────────────────────

def split_utterances(text: str, pattern: str | None = None,
                     accepted: list[str] | None = None) -> list[dict]:
    """
    Convention-aware utterance splitting (named groups via conventions.parse_line,
    legacy positional regexes still work):
    - lines matching the pattern start a turn (speaker normalized);
    - if `accepted` is given (the document's speaker inventory), matches with an
      unknown speaker are demoted to continuation lines — kills false speakers
      like a German sentence starting "Ja. ..." against the f4 separator;
    - continuation lines inherit the current speaker (carry-forward);
    - lines before the first accepted turn are front matter (never coded).
    """
    compiled = re.compile(pattern or DEFAULT_UTTERANCE_REGEX)
    accepted_norm = ({conventions.normalize_speaker(a) for a in accepted}
                     if accepted else None)
    utterances = []
    current_speaker = ""
    seen_turn = False
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        p = conventions.parse_line(line, compiled)
        speaker = conventions.normalize_speaker(p["speaker"]) if p else ""
        is_turn = bool(speaker) and (accepted_norm is None or speaker in accepted_norm)
        if is_turn:
            seen_turn = True
            current_speaker = speaker
            utterances.append({"speaker": speaker, "timestamp": p["timestamp"],
                               "text": p["text"], "front": False})
        else:
            utterances.append({"speaker": current_speaker if seen_turn else "",
                               "timestamp": "", "text": line,
                               "front": not seen_turn})

    # HappyScribe-style: header lines produce empty-text turns; merge each with
    # its first continuation, then drop any residual empty-text entries (e.g. a
    # trailing header at the end of the file with no following text).
    merged: list[dict] = []
    i = 0
    while i < len(utterances):
        u = utterances[i]
        if (not u["text"] and not u["front"]
                and i + 1 < len(utterances)
                and utterances[i + 1]["speaker"] == u["speaker"]
                and not utterances[i + 1]["front"]):
            merged.append({**u, "text": utterances[i + 1]["text"]})
            i += 2
        else:
            merged.append(u)
            i += 1
    return [u for u in merged if u["text"] or u["front"]]


def split_paragraphs(text: str) -> list[dict]:
    """Each non-empty line is one paragraph (load_docx_text joins paragraphs with \\n)."""
    return [
        {"speaker": "", "timestamp": "", "text": line.strip()}
        for line in text.split("\n") if line.strip()
    ]


def split_sentences(text: str, language: str) -> list[dict]:
    nlp = _get_nlp(language)
    segments = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        for sent in nlp(line).sents:
            s = sent.text.strip()
            if s:
                segments.append({"speaker": "", "timestamp": "", "text": s})
    return segments


def segment_text(text: str, mode: str, regex: str | None = None,
                 language: str | None = None) -> list[dict]:
    """Dispatch on workspace segmentation settings."""
    if mode == "utterance_regex":
        return split_utterances(text, regex)
    if mode == "paragraph":
        return split_paragraphs(text)
    if mode == "sentence":
        if not language:
            raise ValueError("sentence mode requires a language")
        return split_sentences(text, language)
    raise ValueError(f"Unknown segmentation mode: {mode}")
