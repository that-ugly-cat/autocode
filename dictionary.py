"""
Dictionary coding engine — ported from FINK NLP-v8.ipynb.

Deterministic, LLM-free: expressions are lemmatized per language and matched
against lemmatized sentences via bag-of-lemmas containment (all the expression's
lemmas must appear in the sentence's lemma set, order-independent).

Two deliberate fixes over the notebook:
- stop words are filtered on BOTH sides (in FINK terms kept their stop words, so
  "cost of care" could never match — the dictionary carried manual stop-word-free
  variants as a workaround);
- case is normalized on both sides (acronyms like SUVA survive the lowercasing).

Matching is always sentence-level inside the coding unit (decision June 2026):
bag-of-lemmas containment degenerates as the window grows, so the unit gets the
code when at least one of its sentences matches.
"""
from collections import defaultdict

from models import Code, CodeExpression
from segmentation import SPACY_MODELS, _get_nlp


def _lemma_bag(tokens) -> list[str]:
    return [t.lemma_.lower() for t in tokens if not t.is_stop and not t.is_punct]


def lemmatize_expression(expression: str, nlp) -> list[str]:
    return _lemma_bag(nlp(expression.lower()))


_QUOTE_OPEN = '"“'   # straight or curly opening quote
_QUOTE_CLOSE = '"”'


def parse_expression(expression: str, nlp) -> tuple[str, list[str]]:
    """
    Two matching semantics, declared by syntax ("option D", June 2026):
    - unquoted  → ("bag", content lemmas): all must appear in a sentence,
                  order-free, stop words excluded — concept co-occurrence;
    - "quoted"  → ("phrase", full lemma sequence): the construction must appear
                  contiguously and in order, stop words KEPT (punctuation dropped) —
                  for idioms where function words are the signal ("on my own").
    Lemma comparison keeps both robust to inflection ("gave up driving").
    """
    expr = expression.strip()
    if len(expr) >= 2 and expr[0] in _QUOTE_OPEN and expr[-1] in _QUOTE_CLOSE:
        inner = expr[1:-1].strip()
        lemmas = [t.lemma_.lower() for t in nlp(inner.lower()) if not t.is_punct]
        return "phrase", lemmas
    return "bag", _lemma_bag(nlp(expr.lower()))


def _contains_sequence(seq: list[str], sub: list[str]) -> bool:
    n, m = len(seq), len(sub)
    if m == 0 or m > n:
        return False
    return any(seq[i:i + m] == sub for i in range(n - m + 1))


def build_index(db, workspace_id: int) -> dict:
    """
    {language: [(code_id, expression, mode, [lemmas])]} for the active codebook.
    Expressions that reduce to nothing are dropped (they would match nothing
    or everything).
    """
    rows = (db.query(CodeExpression)
            .join(Code, Code.id == CodeExpression.code_id)
            .filter(Code.workspace_id == workspace_id, Code.is_deleted == False)
            .all())
    index: dict = {}
    for lang in sorted({r.language for r in rows}):
        if lang not in SPACY_MODELS:
            continue
        nlp = _get_nlp(lang)
        entries = []
        for r in rows:
            if r.language != lang:
                continue
            mode, lemmas = parse_expression(r.expression, nlp)
            if lemmas:
                entries.append((r.code_id, r.expression, mode, lemmas))
        if entries:
            index[lang] = entries
    return index


def match_unit(text: str, language: str, index: dict) -> dict:
    """
    Match one coding unit. Returns {code_id: {"expressions": [...], "score": n,
    "sentences": [...]}} — score = number of (expression, sentence) matches,
    FINK's relevance score.
    """
    entries = index.get(language)
    if not entries:
        return {}
    nlp = _get_nlp(language)
    doc = nlp(text)
    results: dict = defaultdict(lambda: {"expressions": [], "score": 0, "sentences": []})
    for sent in doc.sents:
        # one pass per sentence: ordered lemma sequence (for phrases, stop words
        # kept) and content-lemma set (for bags)
        seq, bag = [], set()
        for t in sent:
            if t.is_punct:
                continue
            lemma = t.lemma_.lower()
            seq.append(lemma)
            if not t.is_stop:
                bag.add(lemma)
        if not seq:
            continue
        for code_id, expression, mode, lemmas in entries:
            if mode == "phrase":
                hit = _contains_sequence(seq, lemmas)
            else:
                hit = bool(bag) and all(lemma in bag for lemma in lemmas)
            if hit:
                r = results[code_id]
                r["score"] += 1
                if expression not in r["expressions"]:
                    r["expressions"].append(expression)
                s = sent.text.strip()
                if s not in r["sentences"]:
                    r["sentences"].append(s)
    return dict(results)


def detect_language(text: str) -> str | None:
    """langdetect on the extracted text; only languages we have models for."""
    try:
        from langdetect import detect
        lang = detect(text[:5000])
        return lang if lang in SPACY_MODELS else None
    except Exception:
        return None
