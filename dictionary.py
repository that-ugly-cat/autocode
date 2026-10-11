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

Signal words (October 2026). spaCy's stop lists include the words that carry an
expression's meaning: negations (nicht, kein, ohne, pas, sans, non, not, no),
indefinite actors (jemand, niemand, nessuno, someone), sameness (gleich, même,
stesso, same) and the like. Dropping them reduced "risque pas vu" to {risque} and
"same doctor" to {doctor}, so the expression fired on any sentence with the topic
word while the run reported the full expression as matched. A validation of run
14 (workspace 6) measured a precision of 0.12 on 953 judged assignments. Signal
words now count as content on both sides; every other stop word is still dropped.
"""
from collections import defaultdict

from models import Code, CodeExpression
from segmentation import SPACY_MODELS, _get_nlp

# Matched against both the token text and its lemma, lowercased. Keep each list to
# words that flip or narrow meaning: a word here must be present for the bag to fire.
SIGNAL_WORDS = {
    "de": "nicht nichts kein keine keinen keiner keinem keines ohne niemand niemanden "
          "niemandem jemand jemanden jemandem nie niemals statt anstatt allein alleine "
          "gleich gleiche gleichen derselbe dieselbe dasselbe wo wen wem noch wieder "
          "zusammen selbst",
    "en": "not no never nothing nobody none without instead someone somebody anyone "
          "anybody same where whom who alone still again together enough only",
    "fr": "pas ne sans aucun aucune personne rien jamais même mêmes seul seule encore "
          "ensemble où qui plus lieu",
    "it": "non nessuno nessuna nessun senza niente nulla mai stesso stessa stessi "
          "invece solo sola ancora insieme dove chi più",
}
SIGNAL_WORDS = {lang: frozenset(words.split()) for lang, words in SIGNAL_WORDS.items()}


def _is_content(token, language: str | None) -> bool:
    if token.is_punct:
        return False
    if not token.is_stop:
        return True
    signal = SIGNAL_WORDS.get(language or "", frozenset())
    text = getattr(token, "text", token.lemma_).lower()
    return text in signal or token.lemma_.lower() in signal


def _token_forms(token) -> set[str]:
    """What a text token can be matched on: its lemma and its own lowercased form."""
    return {token.lemma_.lower(), getattr(token, "text", token.lemma_).lower()}


def _expression_slots(text: str, nlp, language: str | None, keep_stop: bool) -> list[frozenset]:
    """
    One slot per kept word of an expression: the set of forms any of which satisfies it.

    The lemmatizer depends on case, and in German heavily: lowercased, "absprache"
    lemmatizes to "absprach" and "bezugsarzt" to "bezugsarzen", while the capitalized
    nouns of a transcript lemmatize correctly. A validation in October 2026 found 85
    of the 371 German words of one codebook affected, enough to leave a code with no
    hit at all. So each word is lemmatized as typed, lowercased and capitalized, and
    its surface form is accepted too.
    """
    lower = text.lower()
    variants = dict.fromkeys([lower, text, " ".join(w[:1].upper() + w[1:] for w in lower.split())])
    base = list(nlp(lower))
    docs = [d for d in (list(nlp(v)) for v in variants) if len(d) == len(base)]
    slots = []
    for i, t in enumerate(base):
        if t.is_punct or (not keep_stop and not _is_content(t, language)):
            continue
        slots.append(frozenset(set().union(*(_token_forms(d[i]) for d in docs))))
    return slots


def _show(slots: list[frozenset]) -> list[str]:
    return ["/".join(sorted(s)) for s in slots]


def lemmatize_expression(expression: str, nlp, language: str | None = None) -> list[str]:
    return _show(_expression_slots(expression, nlp, language, keep_stop=False))


_QUOTE_OPEN = '"“'   # straight or curly opening quote
_QUOTE_CLOSE = '"”'


def _parse(expression: str, nlp, language: str | None) -> tuple[str, list[frozenset]]:
    expr = expression.strip()
    if len(expr) >= 2 and expr[0] in _QUOTE_OPEN and expr[-1] in _QUOTE_CLOSE:
        return "phrase", _expression_slots(expr[1:-1].strip(), nlp, language, keep_stop=True)
    return "bag", _expression_slots(expr, nlp, language, keep_stop=False)


def parse_expression(expression: str, nlp, language: str | None = None) -> tuple[str, list[str]]:
    """
    Two matching semantics, declared by syntax ("option D", June 2026):
    - unquoted  → ("bag", content lemmas): all must appear in a sentence,
                  order-free, stop words excluded except the language's signal
                  words — concept co-occurrence;
    - "quoted"  → ("phrase", full lemma sequence): the construction must appear
                  contiguously and in order, stop words KEPT (punctuation dropped) —
                  for idioms where function words are the signal ("on my own").
    Lemma comparison keeps both robust to inflection ("gave up driving"). A word
    with several accepted forms (see _expression_slots) is shown as "a/b".
    """
    mode, slots = _parse(expression, nlp, language)
    return mode, _show(slots)


def _contains_sequence(seq: list[set], sub: list[frozenset]) -> bool:
    n, m = len(seq), len(sub)
    if m == 0 or m > n:
        return False
    return any(all(not sub[j].isdisjoint(seq[i + j]) for j in range(m)) for i in range(n - m + 1))


def build_index(db, workspace_id: int) -> dict:
    """
    {language: [(code_id, expression, mode, [slots])]} for the active codebook.
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
            mode, slots = _parse(r.expression, nlp, lang)
            if slots:
                entries.append((r.code_id, r.expression, mode, slots))
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
        # one pass per sentence: ordered forms (for phrases, stop words kept) and
        # the content forms (for bags); a form is a lemma or a lowercased word
        seq, bag = [], set()
        for t in sent:
            if t.is_punct:
                continue
            forms = _token_forms(t)
            seq.append(forms)
            if _is_content(t, language):
                bag |= forms
        if not seq:
            continue
        for code_id, expression, mode, slots in entries:
            if mode == "phrase":
                hit = _contains_sequence(seq, slots)
            else:
                hit = bool(bag) and all(not s.isdisjoint(bag) for s in slots)
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
