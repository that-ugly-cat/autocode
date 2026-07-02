"""
Transcript conventions: presets, line parsing, per-document detection, roles.

A convention is a regex with named groups (?P<speaker>…), (?P<text>…) and an
optional (?P<ts>…). Legacy positional regexes still work (group 1 = speaker,
group 2 = timestamp when 3+ groups, last group = text).

Detection (validated June 2026 against the FINK corpus, ~190 transcripts):
multi-signal scoring — match rate with a MODERATE threshold (multi-paragraph
turns legitimately depress it), speaker plausibility (2–12 distinct, recurring),
timestamp monotonicity. Candidates: built-in presets + the workspace's custom
convention library (the panel teaches new conventions; the detector reuses them).

Roles, not labels: who the interviewer is varies per document (PDI files use
V1/V2/V3). Detection proposes a label→role mapping (interviewer / participant /
other), stored on the document and editable; exclusion in the workspace settings
is by role.
"""
import json
import re
from collections import Counter

ROLES = ("interviewer", "participant", "other")

PRESETS = {
    "default": {
        "label": "SPEAKER [HH:MM:SS]: text (AutoCode / noScribe with timestamps)",
        "regex": r"^(?P<speaker>[^:\[\]]{1,24}?)\s*\[(?P<ts>\d{1,2}:\d{2}:\d{2})\]\s*:\s*(?P<text>.+)$",
    },
    "f4": {
        "label": "SPEAKER: text #h:mm:ss-d# (f4 trailing timestamps)",
        "regex": r"^(?P<speaker>[^\s:.\[\]]{1,24})\s*[:.]\s+(?P<text>.+?)\s*(?:#(?P<ts>\d{1,2}:\d{2}:\d{2})-\d+#)?\s*$",
    },
    "plain": {
        "label": "SPEAKER: text (noScribe default, no timestamps)",
        "regex": r"^(?P<speaker>[^\s:\[\]]{1,24})\s*:\s+(?P<text>.+)$",
    },
    "ts_lead": {
        "label": "[HH:MM:SS] SPEAKER: text",
        "regex": r"^\[(?P<ts>\d{1,2}:\d{2}:\d{2})\]\s+(?P<speaker>[^\s:]{1,24})\s*:\s+(?P<text>.+)$",
    },
    "happyscribe": {
        "label": "[HH:MM:SS.mmm] - SPEAKER / text on next line (HappyScribe)",
        "regex": r"^\[(?P<ts>\d{2}:\d{2}:\d{2})\.\d{3}\]\s*-\s*(?P<speaker>.+?)\s*(?P<text>)$",
    },
}

INTERVIEWER_LABELS = re.compile(r"^(I\d*|IV|INT|MOD(ERATOR)?|INTERVIEWER)$", re.IGNORECASE)

DETECTION_THRESHOLD = 0.45
SAMPLE_LINES = 400


def normalize_speaker(label: str) -> str:
    return re.sub(r"\s+", "", label or "").strip(".:").upper()


def parse_line(line: str, compiled: re.Pattern) -> dict | None:
    m = compiled.match(line)
    if not m:
        return None
    if "speaker" in compiled.groupindex or "text" in compiled.groupindex:
        gd = m.groupdict()
        return {"speaker": (gd.get("speaker") or "").strip(),
                "timestamp": (gd.get("ts") or "").strip(),
                "text": (gd.get("text") or "").strip()}
    groups = m.groups()  # legacy positional contract
    return {"speaker": (groups[0] if groups else "").strip(),
            "timestamp": (groups[1] if len(groups) > 2 else "").strip(),
            "text": (groups[-1] if groups else line).strip()}


def workspace_library(ws) -> dict:
    """{name: regex} of the workspace's custom conventions."""
    try:
        return json.loads(ws.conventions_json or "{}")
    except Exception:
        return {}


def candidates_for(ws) -> list[tuple[str, str]]:
    out = [(name, p["regex"]) for name, p in PRESETS.items()]
    out += list(workspace_library(ws).items())
    return out


def resolve_convention(ws, name: str | None) -> str | None:
    if not name:
        return None
    if name in PRESETS:
        return PRESETS[name]["regex"]
    return workspace_library(ws).get(name)


def _ts_to_seconds(ts: str) -> int:
    parts = [int(p) for p in ts.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _mostly_monotonic(ts_list: list[str]) -> bool:
    if len(ts_list) < 3:
        return False
    secs = [_ts_to_seconds(t) for t in ts_list]
    ok = sum(1 for a, b in zip(secs, secs[1:]) if b >= a)
    return ok >= 0.9 * (len(secs) - 1)


def score_convention(lines: list[str], regex: str) -> dict | None:
    try:
        compiled = re.compile(regex)
    except re.error:
        return None
    speakers: Counter = Counter()
    turn_lines = 0
    ts_list = []
    for line in lines:
        p = parse_line(line, compiled)
        if p and p["speaker"]:
            turn_lines += 1
            speakers[normalize_speaker(p["speaker"])] += 1
            if p["timestamp"]:
                ts_list.append(p["timestamp"])
    if not turn_lines:
        return None
    recurring = {s: c for s, c in speakers.items() if c >= 2}
    rate = turn_lines / len(lines)
    rec_ok = (2 <= len(recurring) <= 12
              and sum(recurring.values()) >= 0.8 * turn_lines)
    ts_ok = _mostly_monotonic(ts_list)
    score = 0.6 * rate + (0.25 if rec_ok else 0.0) + (0.15 if ts_ok else 0.0)
    if "(?P<ts>" in regex:
        score += 0.03  # specificity: prefer timestamp-aware over bare SPEAKER:
    return {"score": round(score, 3), "speakers": sorted(recurring),
            "rate": round(rate, 3), "rec_ok": rec_ok, "ts_ok": ts_ok}


def detect_convention(text: str, candidates: list[tuple[str, str]]) -> dict | None:
    """Best candidate above threshold, or None ('unsegmented')."""
    lines = [l.strip() for l in text.split("\n") if l.strip()][:SAMPLE_LINES]
    if not lines:
        return None
    best = None
    for name, regex in candidates:
        s = score_convention(lines, regex)
        if s and (best is None or s["score"] > best["score"]):
            best = {"name": name, **s}
    if best and best["score"] >= DETECTION_THRESHOLD:
        return best
    return None


def default_roles(speakers: list[str]) -> dict:
    """Conservative defaults: I / IV / INT / MOD → interviewer, rest participant."""
    return {s: ("interviewer" if INTERVIEWER_LABELS.match(s) else "participant")
            for s in speakers}


def validate_custom_regex(regex: str):
    """Raises ValueError unless the regex compiles and has speaker + text groups."""
    try:
        compiled = re.compile(regex)
    except re.error as e:
        raise ValueError(f"Invalid regex: {e}")
    if "speaker" not in compiled.groupindex or "text" not in compiled.groupindex:
        raise ValueError("The regex must define named groups (?P<speaker>...) and (?P<text>...)"
                         " — (?P<ts>...) is optional")


SUGGEST_PROMPT = """\
You are given the first lines of an interview transcript. Write ONE Python regex
that matches the lines where a speaker turn starts, with named groups:
(?P<speaker>...) for the speaker label, (?P<text>...) for the utterance text,
and optionally (?P<ts>...) for a timestamp (hh:mm:ss part only).
Lines that are continuations of the previous turn should NOT match.
Answer with ONLY the regex, no quotes, no explanation.

Transcript sample:
{sample}"""


def suggest_regex(api_key: str, sample: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{"role": "user", "content": SUGGEST_PROMPT.format(sample=sample[:4000])}],
    )
    regex = response.content[0].text.strip().strip("`").strip()
    validate_custom_regex(regex)
    return regex
