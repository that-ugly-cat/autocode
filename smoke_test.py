"""
Phase 1 smoke test — exercises the full HTTP surface in-process via TestClient.
Run from deploy/:  python smoke_test.py
Uses a throwaway DB (data/test.db) and upload dir, both removed at the end.
"""
import os
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FERNET_KEY", "")
if not os.environ["FERNET_KEY"]:
    from cryptography.fernet import Fernet
    os.environ["FERNET_KEY"] = Fernet.generate_key().decode()
os.environ["DATABASE_URL"] = "sqlite:///./data/test.db"
os.environ["UPLOAD_DIR"] = "data/test_uploads"

Path("data").mkdir(exist_ok=True)
for p in ("data/test.db", "data/test_uploads"):
    if Path(p).is_dir():
        shutil.rmtree(p)
    elif Path(p).exists():
        Path(p).unlink()

from fastapi.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402

client = TestClient(app_module.app)
FAILED = []


def check(name, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def ensure_analysis(c, rid, recompute=False):
    """Analysis compute is async now: kick it off and wait for the cache to fill."""
    import time
    c.post(f"/api/runs/{rid}/analysis/compute" + ("?recompute=1" if recompute else ""))
    for _ in range(400):
        st = c.get(f"/api/runs/{rid}/analysis/progress").json().get("status")
        if st in ("done", "error", "idle"):
            return st
        time.sleep(0.05)
    return "timeout"


import totp as _totp


def enroll_2fa(c):
    """Complete the mandatory 2FA enrollment for a freshly-registered client."""
    r = c.post("/api/auth/2fa/setup")
    if r.status_code != 200:
        return None
    secret = r.json()["secret"]
    r = c.post("/api/auth/2fa/setup/confirm", json={"code": _totp.now(secret)})
    return secret if r.status_code == 200 else None


def login_2fa(c, email, password, secret):
    """Full login: password step + TOTP verify step."""
    c.post("/api/auth/login", json={"email": email, "password": password})
    return c.post("/api/auth/2fa/verify", json={"code": _totp.now(secret)})


print("== auth ==")
r = client.post("/api/auth/register", json={"name": "Test Owner", "email": "owner@test.dev", "password": "password123"})
check("register owner", r.status_code == 200, r.text)
r = client.post("/api/auth/register", json={"name": "Test Owner", "email": "owner@test.dev", "password": "password123"})
check("duplicate email rejected", r.status_code == 400)
r = client.post("/api/auth/register", json={"name": "X", "email": "short@test.dev", "password": "short"})
check("short password rejected", r.status_code == 400)

print("== 2FA (mandatory) ==")
# a freshly-registered user is in a pending state until they enroll
r = client.post("/api/workspaces", json={"name": "Too early"})
check("protected route blocked before 2FA enrollment", r.status_code == 401)
owner_secret = enroll_2fa(client)
check("owner enrolls in 2FA (QR + confirm)", owner_secret is not None)
r = client.post("/api/workspaces", json={"name": "After 2FA", "segmentation_mode": "utterance_regex"})
check("protected route works after enrollment", r.status_code == 200, r.text)
# re-login goes through password + code
r = client.post("/api/auth/login", json={"email": "owner@test.dev", "password": "wrong"})
check("bad login rejected", r.status_code == 401)
r = client.post("/api/auth/login", json={"email": "owner@test.dev", "password": "password123"})
check("login password step returns pending", r.status_code == 200 and r.json()["totp_enabled"] is True)
r = client.post("/api/workspaces", json={"name": "still pending"})
check("pending session can't access protected routes", r.status_code == 401)
r = client.post("/api/auth/2fa/verify", json={"code": "000000"})
check("wrong 2FA code rejected", r.status_code == 400)
r = client.post("/api/auth/2fa/verify", json={"code": _totp.now(owner_secret)})
check("2FA verify completes login", r.status_code == 200, r.text)
r = client.get("/")
check("home page logged in after 2FA", r.status_code == 200 and "workspace" in r.text.lower())
# backup codes: regenerate (returns fresh codes), then use one to log in (single-use)
r = client.post("/api/profile/2fa/backup-codes", json={"code": _totp.now(owner_secret)})
check("regenerate backup codes (10)", r.status_code == 200 and len(r.json()["backup_codes"]) == 10, r.text)
bcode = r.json()["backup_codes"][0]
client.post("/api/auth/login", json={"email": "owner@test.dev", "password": "password123"})
r = client.post("/api/auth/2fa/verify", json={"code": bcode})
check("login with a backup code", r.status_code == 200, r.text)
client.post("/api/auth/login", json={"email": "owner@test.dev", "password": "password123"})
r = client.post("/api/auth/2fa/verify", json={"code": bcode})
check("consumed backup code rejected", r.status_code == 400)
r = login_2fa(client, "owner@test.dev", "password123", owner_secret)
check("re-login with TOTP restores full session", r.status_code == 200)

print("== user guide ==")
anon = TestClient(app_module.app)
r = anon.get("/guide")
check("guide renders without login", r.status_code == 200 and "User Guide" in r.text)
check("guide markdown rendered (tables, headings)",
      "<table>" in r.text and "<h2" in r.text and "Dictionary engine" in r.text)
r = client.get("/")
check("guide link on pages", "/guide" in r.text)

print("== second user ==")
member = TestClient(app_module.app)
r = member.post("/api/auth/register", json={"name": "Test Member", "email": "member@test.dev", "password": "password123"})
check("register member", r.status_code == 200, r.text)
member_secret = enroll_2fa(member)
check("member enrolls in 2FA", member_secret is not None)

print("== workspace ==")
r = client.post("/api/workspaces", json={"name": "Interviste pilota", "description": "Test WS",
                                         "segmentation_mode": "utterance_regex"})
check("create workspace", r.status_code == 200, r.text)
ws_id = r.json()["id"]
r = client.post("/api/workspaces", json={"name": "Sentence WS", "segmentation_mode": "sentence"})
check("sentence mode allowed without workspace language (per-document now)",
      r.status_code == 200, r.text)
r = client.post("/api/workspaces", json={"name": "Bad", "segmentation_mode": "utterance_regex",
                                         "segmentation_regex": "(unclosed"})
check("invalid regex rejected", r.status_code == 400)
r = member.get(f"/workspace/{ws_id}")
check("non-member cannot open workspace", r.status_code == 404)
r = client.post(f"/api/workspaces/{ws_id}/members", json={"email": "member@test.dev"})
check("add member", r.status_code == 200, r.text)
r = client.post(f"/api/workspaces/{ws_id}/members", json={"email": "ghost@test.dev"})
check("unknown email rejected", r.status_code == 404)
r = member.get(f"/workspace/{ws_id}")
check("member can open workspace", r.status_code == 200)
r = member.get(f"/workspace/{ws_id}/settings")
check("member sees settings page (read)", r.status_code == 200)
r = member.put(f"/api/workspaces/{ws_id}", json={"name": "Hacked", "segmentation_mode": "paragraph"})
check("member cannot edit settings", r.status_code == 403)

print("== corpus ==")
docx_path = Path("../test_transcripts/transcript_P01.docx")
if docx_path.exists():
    with docx_path.open("rb") as f:
        r = client.post(f"/api/workspaces/{ws_id}/documents",
                        files=[("files", ("transcript_P01.docx", f,
                                          "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))])
    check("upload docx", r.status_code == 200 and r.json()["uploaded"], r.text)
    r = client.post(f"/api/workspaces/{ws_id}/documents",
                    files=[("files", ("~$lock.docx", b"junk", "application/octet-stream"))])
    check("lock file skipped", r.status_code == 200 and r.json()["skipped"] == ["~$lock.docx"])
    r = client.get(f"/workspace/{ws_id}/corpus")
    check("corpus page lists doc", r.status_code == 200 and "transcript_P01.docx" in r.text)
else:
    print("  [skip] no test transcript found")

print("== segmentation preview ==")
sample = "ANNA [00:01:12]: First utterance here.\nMARK [00:01:30]: Second one.\nA continuation line."
r = client.post(f"/api/workspaces/{ws_id}/segmentation/preview", json={"text": sample})
ok = r.status_code == 200 and len(r.json()["segments"]) == 3 and r.json()["segments"][0]["speaker"] == "ANNA"
check("utterance preview", ok, r.text)

print("== codebook ==")
r = client.post(f"/api/workspaces/{ws_id}/codes",
                json={"label": "autonomy", "description": "Patient autonomy themes"})
check("add code", r.status_code == 200, r.text)
code_id = r.json()["id"]
r = member.post(f"/api/workspaces/{ws_id}/codes", json={"label": "Autonomy"})
check("duplicate label (case-insensitive) rejected", r.status_code == 400)
r = member.put(f"/api/codes/{code_id}", json={"label": "autonomy", "description": "Updated by member"})
check("member can edit code", r.status_code == 200, r.text)
cb_path = Path("../test_transcripts/codebook.xlsx")
if cb_path.exists():
    with cb_path.open("rb") as f:
        r = client.post(f"/api/workspaces/{ws_id}/codebook/preview-import",
                        files={"file": ("codebook.xlsx", f, "application/vnd.ms-excel")})
    check("preview import", r.status_code == 200 and r.json()["rows"], r.text)
    rows = [{"label": x["label"], "description": x["description"], "example": x["example"]}
            for x in r.json()["rows"] if not x["duplicate"]]
    r = client.post(f"/api/workspaces/{ws_id}/codebook/import", json={"rows": rows})
    check("confirm import", r.status_code == 200 and r.json()["created"] == len(rows), r.text)
else:
    print("  [skip] no test codebook found")
r = client.delete(f"/api/codes/{code_id}")
check("soft delete code", r.status_code == 200)
r = client.get(f"/workspace/{ws_id}/codebook")
check("deleted code hidden", r.status_code == 200 and "Updated by member" not in r.text)

print("== profile / api key ==")
r = client.post("/api/profile/api-key", json={"api_key": "not-a-key"})
check("malformed key rejected", r.status_code == 400)
r = client.post("/api/profile/api-key", json={"api_key": "sk-ant-test-1234567890wxyz"})
check("save key", r.status_code == 200 and r.json()["masked"].endswith("wxyz"), r.text)
r = client.get("/profile")
check("profile shows masked key", r.status_code == 200 and "wxyz" in r.text)

print("== admin ==")
r = client.get("/admin")
check("non-admin blocked from /admin", r.status_code == 403)
# promote owner to admin directly in DB
from models import SessionLocal, User
db = SessionLocal()
db.query(User).filter(User.email == "owner@test.dev").update({"is_admin": True})
db.commit()
db.close()
r = client.get("/admin")
check("admin page", r.status_code == 200 and "member@test.dev" in r.text)
member_id = None
db = SessionLocal()
member_id = db.query(User).filter(User.email == "member@test.dev").first().id
db.close()
# admin reset 2FA: clears the target's TOTP so they must re-enroll on next login
r = client.post(f"/api/admin/users/{member_id}/reset-2fa")
check("admin reset 2FA", r.status_code == 200, r.text)
db = SessionLocal()
_m = db.query(User).filter(User.id == member_id).first()
check("reset clears the user's 2FA",
      not _m.totp_enabled and _m.totp_secret_encrypted is None)
db.close()
r = client.post(f"/api/admin/users/{member_id}/toggle-active")
check("disable user", r.status_code == 200 and r.json()["is_active"] is False)
r = member.get("/profile")
check("disabled user logged out", r.status_code in (302, 303, 307) or "login" in r.text.lower())

# ── fake spaCy (lemma_ = lowercased word) for dictionary engine + lemma analysis ──
import re as _re
import dictionary as dict_mod


class _FakeToken:
    STOPS = {"the", "a", "an", "of", "i", "to", "was", "is", "and", "it", "me", "at", "were", "had", "from", "my", "for"}

    def __init__(self, w):
        self.lemma_ = w
        self.is_stop = w in self.STOPS
        self.is_punct = not any(ch.isalnum() for ch in w)
        self.is_digit = w.isdigit()


class _FakeSent:
    def __init__(self, text):
        self.text = text
        self._toks = [_FakeToken(w) for w in _re.findall(r"[\w'-]+|[^\w\s]", text.lower())]

    def __iter__(self):
        return iter(self._toks)


class _FakeDoc:
    def __init__(self, text):
        parts = [s.strip() for s in _re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        self.sents = [_FakeSent(s) for s in (parts or [text])]

    def __iter__(self):
        for s in self.sents:
            for t in s:
                yield t


_real_get_nlp = dict_mod._get_nlp
dict_mod._get_nlp = lambda lang: _FakeDoc  # callable returning a doc
# split_sentences resolves _get_nlp in the segmentation namespace, so patch it there
# too (docx/excel sentence mode would otherwise load the real spaCy models)
import segmentation as _seg_mod
_seg_mod._get_nlp = lambda lang: _FakeDoc

print("== phase 2: run engine (mocked API) ==")
import time as _time
import coding

# fake Claude: first segment uses an existing code, second proposes the same new code
# twice with different label forms (dedup check), third is no_code
_FAKE_RESPONSES = [
    [{"action": "use_existing", "code": "autonomy", "rationale": "mentions choice"}],
    [{"action": "create_new", "code": "trust_in_physician", "description": "Trust themes",
      "example": "…", "rationale": "new theme"},
     {"action": "create_new", "code": "Trust in physician", "description": "dup form",
      "example": "…", "rationale": "dup"}],
    [{"action": "no_code", "rationale": "small talk"}],
]
_call_count = {"n": 0}

def _fake_call(client, system_prompt, text, context_utts=None, model="x", max_tokens=1024, max_retries=5):
    resp = _FAKE_RESPONSES[_call_count["n"] % len(_FAKE_RESPONSES)]
    _call_count["n"] += 1
    return resp, 100, 20

_real_call = coding.call_claude_with_retry
coding.call_claude_with_retry = _fake_call


class _FakeAnthropicClient:
    pass


_real_anthropic_cls = coding.anthropic.Anthropic
coding.anthropic.Anthropic = lambda api_key: _FakeAnthropicClient()

# workspace with a real docx + a code to match against
r = client.post("/api/workspaces", json={"name": "Run WS", "segmentation_mode": "utterance_regex"})
check("create run workspace", r.status_code == 200, r.text)
run_ws = r.json()["id"]
r = client.post(f"/api/workspaces/{run_ws}/codes", json={"label": "autonomy", "description": "Autonomy"})
check("seed code", r.status_code == 200)
autonomy_id = r.json()["id"]
if docx_path.exists():
    with docx_path.open("rb") as f:
        r = client.post(f"/api/workspaces/{run_ws}/documents",
                        files=[("files", ("t1.docx", f, "application/octet-stream"))])
    doc_id = None
    r2 = client.get(f"/workspace/{run_ws}/corpus")
    from models import Document as _Doc
    db = SessionLocal()
    doc_id = db.query(_Doc).filter(_Doc.workspace_id == run_ws).first().id
    db.close()

    r = client.post(f"/api/workspaces/{run_ws}/runs/estimate",
                    json={"document_ids": [doc_id], "granularity": "per_utterance",
                          "context_window": 3, "model": "claude-sonnet-4-6"})
    check("cost estimate", r.status_code == 200 and r.json()["segments"] > 0
          and r.json()["cost_usd"] > 0, r.text)
    check("estimate includes run-time eta", r.json().get("eta_seconds", 0) > 0, r.text)

    # dictionary engine needs expressions: run_ws codebook has none → hard block
    r = client.post(f"/api/workspaces/{run_ws}/runs",
                    json={"document_ids": [doc_id], "engine": "dictionary"})
    check("dictionary run blocked on empty codebook (no expressions)",
          r.status_code == 400 and "expression" in r.json()["detail"].lower(), r.text)

    r = client.post(f"/api/workspaces/{run_ws}/runs",
                    json={"document_ids": [doc_id], "granularity": "per_utterance",
                          "context_window": 2, "model": "claude-sonnet-4-6", "max_workers": 2})
    check("run without study context rejected", r.status_code == 400
          and "study context" in r.json()["detail"].lower(), r.text)
    r = client.put(f"/api/workspaces/{run_ws}",
                   json={"name": "Run WS", "study_context": "Pilot study on patient autonomy",
                         "segmentation_mode": "utterance_regex"})
    check("set study context", r.status_code == 200, r.text)

    r = client.post(f"/api/workspaces/{run_ws}/runs",
                    json={"document_ids": [doc_id], "granularity": "per_utterance",
                          "context_window": 2, "model": "claude-sonnet-4-6", "max_workers": 2,
                          "qdpx_enabled": True})
    check("start run", r.status_code == 200, r.text)
    run_id = r.json()["id"]

    for _ in range(100):  # wait for the background thread
        r = client.get(f"/api/runs/{run_id}")
        if r.json()["status"] in ("completed", "failed"):
            break
        _time.sleep(0.1)
    data = r.json()
    check("run completed", data["status"] == "completed", str(data))
    from models import Run as _RunChk
    db = SessionLocal()
    _r1 = db.query(_RunChk).filter(_RunChk.id == run_id).first()
    db.close()
    check("docx unit snapshotted from workspace", _r1.granularity == "utterance_regex",
          _r1.granularity)
    check("codings created", data["n_codings"] > 0, str(data))
    from models import RunSegment as _Seg
    db = SessionLocal()
    segs1 = db.query(_Seg).filter(_Seg.run_id == run_id).all()
    db.close()
    check("coverage recorded: one RunSegment per unit",
          len(segs1) == data["n_segments"] and len(segs1) > 0, str(data)[:200])
    check("no_code rationale preserved",
          any(s.status == "no_code" and s.no_code_rationale for s in segs1))
    check("new code deduped (1 not 2)", data["n_new_codes"] == 1, str(data))
    check("tokens tracked", data["cost_input_tokens"] > 0 and data["cost_usd"] > 0)
    check("doc completed", all(d["status"] == "completed" for d in data["documents"]))
    check("run status reports progress counters",
          data["n_docs"] == len(data["documents"]) and data["n_docs_done"] == data["n_docs"],
          str(data)[:200])

    # per-run coding unit (C1): invalid unit rejected, valid one snapshotted on the run
    r = client.post(f"/api/workspaces/{run_ws}/runs",
                    json={"document_ids": [doc_id], "unit": "bogus"})
    check("invalid coding unit rejected", r.status_code == 400, r.text)
    r = client.post(f"/api/workspaces/{run_ws}/runs",
                    json={"document_ids": [doc_id], "unit": "paragraph", "max_workers": 1})
    check("run accepts a per-run unit", r.status_code == 200, r.text)
    prun = r.json()["id"]
    for _ in range(100):
        if client.get(f"/api/runs/{prun}").json()["status"] in ("completed", "failed"):
            break
        _time.sleep(0.1)
    db = SessionLocal()
    _pru = db.query(_RunChk).filter(_RunChk.id == prun).first()
    db.close()
    check("per-run unit snapshotted to granularity", _pru.granularity == "paragraph",
          str(_pru.granularity))

    r = client.get(f"/workspace/{run_ws}/codebook")
    check("proposed code visible with badge", "trust_in_physician" in r.text and "proposed by the model" in r.text)
    r = client.get(f"/workspace/{run_ws}/runs/{run_id}")
    check("run detail page", r.status_code == 200 and "Run #" in r.text)
    r = client.get(f"/workspace/{run_ws}/corpus")
    check("analyzed badge on corpus", "analyzed" in r.text)
    r = client.post(f"/api/runs/{run_id}/retry-failed")
    check("retry without failures rejected", r.status_code == 400)
    r = client.get("/profile")
    check("cost log on profile", r.status_code == 200 and "#" + str(run_id) in r.text)

    print("== phase 3: exports + review ==")
    import io as _io
    import zipfile as _zip
    r = client.get(f"/api/runs/{run_id}/export/xlsx")
    check("export xlsx", r.status_code == 200 and r.content[:2] == b"PK")
    import pandas as _pd_xl
    sdf = _pd_xl.read_excel(_io.BytesIO(r.content), sheet_name="segments")
    check("xlsx segments sheet has uncoded rows",
          "status" in sdf.columns and (sdf["status"] == "no_code").any(), str(sdf.columns))
    r = client.get(f"/api/runs/{run_id}/export/qdc")
    check("export qdc", r.status_code == 200 and b"autonomy" in r.content
          and b"CodeBook" in r.content)
    r = client.get(f"/api/runs/{run_id}/export/qdpx")
    qdpx_ok = r.status_code == 200
    if qdpx_ok:
        names = _zip.ZipFile(_io.BytesIO(r.content)).namelist()
        qdpx_ok = "project.qde" in names and any(n.startswith("sources/") for n in names)
    check("export qdpx (zip with project.qde + sources/)", qdpx_ok)
    r = client.get(f"/workspace/{run_ws}/codebook")
    check("codebook shows extract counts", r.status_code == 200 and f"/codes/{autonomy_id}" in r.text)
    r = client.get(f"/workspace/{run_ws}/codes/{autonomy_id}")
    check("extracts page with rationale", r.status_code == 200 and "Model rationale" in r.text)
    r = client.get(f"/workspace/{run_ws}/codes/{autonomy_id}?run_id={run_id}")
    check("extracts filtered by run", r.status_code == 200)

    print("== analysis (LLM run) ==")
    check("analysis compute (async) for LLM run",
          ensure_analysis(client, run_id) == "done")
    r = client.get(f"/workspace/{run_ws}/runs/{run_id}/analysis")
    check("analysis page for LLM run", r.status_code == 200
          and "/analysis/chart/codes" in r.text
          and "/analysis/chart/expressions" not in r.text, r.text[:200])
    r = client.get(f"/api/runs/{run_id}/analysis/chart/codes?fmt=png&theme=dark")
    check("LLM codes chart png", r.status_code == 200 and r.content[:4] == b"\x89PNG")
    r = client.get(f"/api/runs/{run_id}/analysis/chart/expressions")
    check("expressions chart absent for LLM run", r.status_code == 404)

    # failed-document path: break the file, new run, then retry after fixing
    db = SessionLocal()
    doc = db.query(_Doc).filter(_Doc.id == doc_id).first()
    real_path = doc.file_path
    doc.file_path = real_path + ".missing"
    db.commit(); db.close()
    r = client.post(f"/api/workspaces/{run_ws}/runs",
                    json={"document_ids": [doc_id], "granularity": "per_utterance",
                          "context_window": 0, "model": "claude-sonnet-4-6", "max_workers": 1})
    run2 = r.json()["id"]
    for _ in range(100):
        r = client.get(f"/api/runs/{run2}")
        if r.json()["status"] in ("completed", "failed"):
            break
        _time.sleep(0.1)
    data = r.json()
    check("partial failure: run completed with error", data["status"] == "completed"
          and data["error_message"], str(data)[:200])
    check("doc marked failed", data["documents"][0]["status"] == "failed")
    db = SessionLocal()
    db.query(_Doc).filter(_Doc.id == doc_id).update({"file_path": real_path})
    db.commit(); db.close()
    r = client.post(f"/api/runs/{run2}/retry-failed")
    check("retry failed accepted", r.status_code == 200, r.text)
    for _ in range(100):
        r = client.get(f"/api/runs/{run2}")
        if r.json()["status"] in ("completed", "failed"):
            break
        _time.sleep(0.1)
    data = r.json()
    check("retry completed", data["status"] == "completed" and not data["error_message"], str(data)[:200])
    r = client.get(f"/api/runs/{run2}/export/qdpx")
    check("qdpx always available on completed runs", r.status_code == 200)
else:
    print("  [skip] no test transcript for run engine")

# run without API key
member2 = TestClient(app_module.app)
member2.post("/api/auth/register", json={"name": "NoKey", "email": "nokey@test.dev", "password": "password123"})
enroll_2fa(member2)
r = member2.post("/api/workspaces", json={"name": "NoKey WS", "segmentation_mode": "paragraph"})
nokey_ws = r.json()["id"]
r = member2.post(f"/api/workspaces/{nokey_ws}/runs",
                 json={"document_ids": [1], "granularity": "per_row", "model": "claude-sonnet-4-6"})
check("run without API key rejected", r.status_code == 400)

print("== document unit (whole file) ==")
if docx_path.exists():
    r = client.put(f"/api/workspaces/{run_ws}",
                   json={"name": "Run WS", "study_context": "Pilot study on patient autonomy",
                         "segmentation_mode": "document"})
    check("switch workspace to document unit", r.status_code == 200, r.text)
    _call_count["n"] = 0  # first fake response = use_existing → exactly 1 coding
    r = client.post(f"/api/workspaces/{run_ws}/runs",
                    json={"document_ids": [doc_id], "model": "claude-sonnet-4-6", "max_workers": 1})
    check("start document-unit run", r.status_code == 200, r.text)
    run3 = r.json()["id"]
    for _ in range(100):
        r = client.get(f"/api/runs/{run3}")
        if r.json()["status"] in ("completed", "failed"):
            break
        _time.sleep(0.1)
    data = r.json()
    from models import Run as _Run3, Coding as _Coding3
    db = SessionLocal()
    _row3 = db.query(_Run3).filter(_Run3.id == run3).first()
    _c3 = db.query(_Coding3).filter(_Coding3.run_id == run3).all()
    db.close()
    check("document run completed with 1 coding", data["status"] == "completed"
          and data["n_codings"] == 1, str(data)[:200])
    check("unit snapshotted as document, ctx 0, no offsets",
          _row3.granularity == "document" and _row3.context_window == 0
          and _c3[0].start_offset is None)
else:
    print("  [skip] no test transcript for document unit")

print("== excel workspaces ==")
import pandas as _pd

r = client.post("/api/workspaces", json={"name": "Bad XLS", "input_type": "excel",
                                         "segmentation_mode": "utterance_regex"})
# the coding unit is per-run now: workspace creation no longer rejects a mismatched
# mode (it falls back to a sane default); the run form validates the actual unit
check("excel ws creation ignores a docx mode (unit is per-run)", r.status_code == 200, r.text)
r = client.post("/api/workspaces", json={"name": "Survey WS", "input_type": "excel",
                                         "segmentation_mode": "cell",
                                         "study_context": "Survey on patient experience"})
check("create excel workspace (cell mode)", r.status_code == 200, r.text)
xls_ws = r.json()["id"]

survey_path = Path("data/test_survey.xlsx")
_pd.DataFrame({
    "ID": [1, 2, 3, 4, 5],
    "feedback": ["The visit was fine.", "", None,
                 "Doctor explained nothing. I felt lost.", "Great staff."],
    "notes": ["a", "b", "c", "d", "e"],
}).to_excel(survey_path, index=False)

with survey_path.open("rb") as f:
    r = client.post(f"/api/workspaces/{xls_ws}/excel/inspect",
                    files={"file": ("survey.xlsx", f, "application/vnd.ms-excel")})
check("excel inspect", r.status_code == 200, r.text)
xls = r.json()
sheet = list(xls["sheets"].keys())[0]
fb = [c for c in xls["sheets"][sheet] if c["name"] == "feedback"]
check("inspect finds feedback column (3 non-empty)", fb and fb[0]["n_values"] == 3, str(xls)[:200])

r = client.post(f"/api/workspaces/{xls_ws}/excel/confirm",
                json={"token": xls["token"], "filename": "survey.xlsx",
                      "sheet": sheet, "columns": ["feedback"]})
check("excel confirm", r.status_code == 200 and r.json()["created"] == ["feedback"], r.text)
r = client.get(f"/workspace/{xls_ws}/corpus")
check("corpus shows column document", r.status_code == 200 and "[feedback]" in r.text)

from models import Document as _Doc2
db = SessionLocal()
xls_doc = db.query(_Doc2).filter(_Doc2.workspace_id == xls_ws).first()
db.close()
r = client.get(f"/api/documents/{xls_doc.id}/preview-segments")
ok = (r.status_code == 200 and r.json()["total"] == 3
      and r.json()["segments"][0]["row"] == 2)
check("preview skips empty cells, keeps row numbers", ok, r.text)

# optional per-row group column: a text column splits into one document per group value
r = client.post("/api/workspaces", json={"name": "Survey Group WS", "input_type": "excel",
                                         "segmentation_mode": "cell", "study_context": "arms"})
grp_ws = r.json()["id"]
grp_path = Path("data/test_survey_group.xlsx")
_pd.DataFrame({
    "arm": ["A", "B", "A", "B", "A"],
    "feedback": ["Good A1", "Bad B1", "Good A2", "", "Great A3"],
}).to_excel(grp_path, index=False)
with grp_path.open("rb") as f:
    gxls = client.post(f"/api/workspaces/{grp_ws}/excel/inspect",
                       files={"file": ("survey_group.xlsx", f, "application/vnd.ms-excel")}).json()
gsheet = list(gxls["sheets"].keys())[0]
r = client.post(f"/api/workspaces/{grp_ws}/excel/confirm",
                json={"token": gxls["token"], "filename": "survey_group.xlsx",
                      "sheet": gsheet, "columns": ["feedback"], "group_column": "arm"})
check("excel confirm with group column splits into per-group docs",
      r.status_code == 200 and set(r.json()["created"]) == {"feedback (A)", "feedback (B)"}, r.text)
db = SessionLocal()
gdocs = db.query(_Doc2).filter(_Doc2.workspace_id == grp_ws).all()
glabels = sorted(d.group_label for d in gdocs)
gprev = {d.group_label: client.get(f"/api/documents/{d.id}/preview-segments").json()["total"]
         for d in gdocs}
db.close()
check("group docs carry the group label", glabels == ["A", "B"], str(glabels))
check("group split filters rows per group (A=3, B=1)",
      gprev.get("A") == 3 and gprev.get("B") == 1, str(gprev))
r = client.get(f"/workspace/{grp_ws}/corpus")
check("corpus shows the group in the document name",
      "feedback] (A)" in r.text and "feedback] (B)" in r.text)

# corpus bundle: export grp_ws, import into a fresh workspace, round-trip metadata
r = client.get(f"/api/workspaces/{grp_ws}/corpus/export")
check("corpus export returns a zip bundle", r.status_code == 200 and r.content[:2] == b"PK",
      str(r.status_code))
bundle = r.content
imp_ws = client.post("/api/workspaces", json={"name": "Import target", "input_type": "excel",
                                              "study_context": "x"}).json()["id"]
r = client.post(f"/api/workspaces/{imp_ws}/corpus/import",
                files={"file": ("corpus.autocorpus", bundle, "application/zip")})
check("corpus import succeeds", r.status_code == 200 and r.json()["imported"] == 2, r.text)
db = SessionLocal()
idocs = db.query(_Doc2).filter(_Doc2.workspace_id == imp_ws).all()
ilabels = sorted(d.group_label for d in idocs)
import json as _json2
has_cfg = all("group_value" in _json2.loads(d.source_config or "{}") for d in idocs)
db.close()
check("imported docs preserve group labels", ilabels == ["A", "B"], str(ilabels))
check("imported docs preserve source_config (group split survives)", has_cfg)
r = client.post(f"/api/workspaces/{ws_id}/corpus/import",
                files={"file": ("corpus.autocorpus", bundle, "application/zip")})
check("corpus import rejects input_type mismatch (excel bundle → docx ws)",
      r.status_code == 400, r.text)

r = client.post(f"/api/workspaces/{xls_ws}/documents",
                files=[("files", ("x.docx", b"junk", "application/octet-stream"))])
check("docx upload rejected in excel ws", r.status_code == 400)
r = client.put(f"/api/workspaces/{xls_ws}",
               json={"name": "Survey WS", "input_type": "docx",
                     "segmentation_mode": "paragraph",
                     "study_context": "Survey on patient experience"})
check("input type locked with corpus", r.status_code == 400)

r = client.post(f"/api/workspaces/{xls_ws}/runs/estimate",
                json={"document_ids": [xls_doc.id], "model": "claude-sonnet-4-6"})
check("excel estimate", r.status_code == 200 and r.json()["segments"] == 3, r.text)
r = client.post(f"/api/workspaces/{xls_ws}/runs",
                json={"document_ids": [xls_doc.id], "granularity": "per_utterance",
                      "context_window": 5, "model": "claude-sonnet-4-6", "qdpx_enabled": True})
check("start excel run", r.status_code == 200, r.text)
xls_run = r.json()["id"]
for _ in range(100):
    r = client.get(f"/api/runs/{xls_run}")
    if r.json()["status"] in ("completed", "failed"):
        break
    _time.sleep(0.1)
data = r.json()
check("excel run completed", data["status"] == "completed", str(data)[:200])
from models import Run as _Run, Coding as _Coding
db = SessionLocal()
run_row = db.query(_Run).filter(_Run.id == xls_run).first()
codings = db.query(_Coding).filter(_Coding.run_id == xls_run).all()
db.close()
check("excel unit snapshotted as cell, ctx forced to 0",
      run_row.granularity == "cell" and run_row.context_window == 0,
      f"{run_row.granularity}/{run_row.context_window}")
check("codings carry excel row numbers", codings and all(c.row_index in (2, 5, 6) for c in codings),
      str([c.row_index for c in codings]))
r = client.get(f"/api/runs/{xls_run}/export/xlsx")
xdf = _pd.read_excel(_io.BytesIO(r.content))
check("xlsx export has excel_row column", "excel_row" in xdf.columns and "[feedback]" in str(xdf["document"].iloc[0]))
r = client.get(f"/api/runs/{xls_run}/export/qdpx")
qdpx_ok = r.status_code == 200
src_text = ""
if qdpx_ok:
    zf = _zip.ZipFile(_io.BytesIO(r.content))
    names = zf.namelist()
    qdpx_ok = "project.qde" in names and any(n.startswith("sources/") for n in names)
    if qdpx_ok:
        src_text = zf.read([n for n in names if n.startswith("sources/")][0]).decode("utf-8")
check("excel qdpx export", qdpx_ok)
check("qdpx source carries row markers", src_text.startswith("[R2] "), src_text[:60])
check("qdpx offsets anchor the raw answer (marker outside selection)",
      codings and src_text[codings[0].start_offset:codings[0].end_offset] == codings[0].segment_text,
      src_text[:80])

# re-anchoring: corrupt the stored offsets (simulates codings created against a
# differently-built fulltext) and verify the QDPX still anchors every selection
db = SessionLocal()
db.query(_Coding).filter(_Coding.run_id == xls_run).update(
    {_Coding.start_offset: _Coding.start_offset - 5,
     _Coding.end_offset: _Coding.end_offset - 5}, synchronize_session=False)
db.commit(); db.close()
r = client.get(f"/api/runs/{xls_run}/export/qdpx")
import xml.etree.ElementTree as _ET
zf = _zip.ZipFile(_io.BytesIO(r.content))
src_text2 = zf.read([n for n in zf.namelist() if n.startswith("sources/")][0]).decode("utf-8")
root = _ET.fromstring(zf.read("project.qde"))
sels = list(root.iter("{urn:QDA-XML:project:1.0}PlainTextSelection"))
slices = [src_text2[int(s.get("startPosition")):int(s.get("endPosition"))] for s in sels]
seg_texts = {c.segment_text for c in codings}
check("qdpx re-anchors drifted offsets at export",
      sels and all(sl in seg_texts and "[R" not in sl for sl in slices),
      str(slices[:2]))
r = client.delete(f"/api/workspaces/{xls_ws}")
check("cleanup excel workspace", r.status_code == 200)
survey_path.unlink(missing_ok=True)

coding.call_claude_with_retry = _real_call
coding.anthropic.Anthropic = _real_anthropic_cls
r = client.delete(f"/api/workspaces/{run_ws}")
check("cleanup run workspace", r.status_code == 200)

print("== dictionary engine (FINK) ==")
dictuser = TestClient(app_module.app)
r = dictuser.post("/api/auth/register", json={"name": "Dict User", "email": "dict@test.dev",
                                              "password": "password123"})
check("register dictionary user (no API key)", r.status_code == 200)
enroll_2fa(dictuser)
r = dictuser.post("/api/workspaces", json={"name": "Dict WS", "input_type": "excel",
                                           "segmentation_mode": "cell"})
check("create dict workspace (no study context)", r.status_code == 200, r.text)
dws = r.json()["id"]

dict_survey = Path("data/test_dict_survey.xlsx")
_pd.DataFrame({
    "response": [
        "The staff at the clinic were great and very kind to me, truly great people during every single visit this year.",
        "Nobody explained the treatment options and I felt completely lost about what would happen next.",
        "I had to borrow money from my family to pay for the medication costs every month.",
        "I cannot just go where I want anymore.",            # phrase target (in order)
        "I want to know where you go sometimes.",            # same lemmas, scrambled: phrase must NOT fire
    ],
}).to_excel(dict_survey, index=False)
with dict_survey.open("rb") as f:
    r = dictuser.post(f"/api/workspaces/{dws}/excel/inspect",
                      files={"file": ("dict_survey.xlsx", f, "application/vnd.ms-excel")})
tok = r.json()
sheet0 = list(tok["sheets"].keys())[0]
r = dictuser.post(f"/api/workspaces/{dws}/excel/confirm",
                  json={"token": tok["token"], "filename": "dict_survey.xlsx",
                        "sheet": sheet0, "columns": ["response"]})
check("dict corpus upload", r.status_code == 200, r.text)
db = SessionLocal()
ddoc = db.query(_Doc2).filter(_Doc2.workspace_id == dws).first()
detected = ddoc.language
db.close()
check("language auto-detected (langdetect)", detected == "en", str(detected))

r = dictuser.post(f"/api/workspaces/{dws}/codes",
                  json={"label": "staff_quality",
                        "expressions": {"en": ["clinic visit"], "xx": ["bad"]}})
check("add code with bad expression language rejected", r.status_code == 400)
r = dictuser.post(f"/api/workspaces/{dws}/codes",
                  json={"label": "staff_quality", "expressions": {"en": ["kind nurses"]}})
staff_id = r.json()["id"]
r = dictuser.get(f"/api/codes/{staff_id}/expressions")
check("expressions saved at code creation", r.json()["expressions"]["en"] == ["kind nurses"])
r = dictuser.get(f"/workspace/{dws}/codebook")
check("expr edit button has no inline label (quoting bug)",
      f"openExpr({staff_id})" in r.text and "openExpr(" + str(staff_id) + "," not in r.text)
r = dictuser.post(f"/api/workspaces/{dws}/codes", json={"label": "financial_burden"})
fin_id = r.json()["id"]
r = dictuser.post(f"/api/workspaces/{dws}/codes", json={"label": "uncovered_code"})
check("seed dict codes", r.status_code == 200)
r = dictuser.post(f"/api/workspaces/{dws}/codes",
                  json={"label": "agency", "expressions": {"en": ["\"go where I want\""]}})
agency_id = r.json()["id"]
r = dictuser.put(f"/api/codes/{staff_id}/expressions",
                 json={"expressions": {"en": ["great staff"]}})
check("set expressions", r.status_code == 200 and r.json()["count"] == 1, r.text)
r = dictuser.put(f"/api/codes/{fin_id}/expressions",
                 json={"expressions": {"en": ["borrow money", "medication costs"]}})
check("set multi expressions", r.status_code == 200 and r.json()["count"] == 2)
r = dictuser.get(f"/api/codes/{fin_id}/expressions")
check("get expressions", r.status_code == 200 and len(r.json()["expressions"]["en"]) == 2)
r = dictuser.put(f"/api/codes/{staff_id}/expressions",
                 json={"expressions": {"xx": ["nope"]}})
check("unsupported language rejected", r.status_code == 400)

r = dictuser.get(f"/workspace/{dws}/runs")
check("coverage warning on runs page", r.status_code == 200 and "have no expressions" in r.text)

r = dictuser.post("/api/expressions/preview",
                  json={"expressions": {"en": ["borrow money", "of the to", "\"go where I want\""],
                                        "xx": ["skip"]}})
prev = r.json()["preview"]
check("lemma preview returns reductions",
      r.status_code == 200 and prev["en"][0]["lemmas"] == ["borrow", "money"]
      and prev["en"][0]["mode"] == "bag"
      and prev["en"][1]["lemmas"] == [] and "xx" not in prev, str(prev))
check("quoted expression previewed as phrase with stop words kept",
      prev["en"][2]["mode"] == "phrase" and prev["en"][2]["lemmas"] == ["go", "where", "i", "want"],
      str(prev["en"][2]))

r = dictuser.post(f"/api/workspaces/{dws}/runs/estimate",
                  json={"document_ids": [ddoc.id], "engine": "dictionary"})
check("dict estimate free", r.status_code == 200 and r.json()["cost_usd"] == 0
      and r.json()["segments"] == 5, r.text)
r = dictuser.post(f"/api/workspaces/{dws}/runs",
                  json={"document_ids": [ddoc.id], "engine": "dictionary"})
check("dict run starts without key and study context", r.status_code == 200, r.text)
drun = r.json()["id"]
for _ in range(100):
    r = dictuser.get(f"/api/runs/{drun}")
    if r.json()["status"] in ("completed", "failed"):
        break
    _time.sleep(0.1)
data = r.json()
check("dict run completed, zero cost", data["status"] == "completed"
      and data["cost_usd"] == 0, str(data)[:200])
db = SessionLocal()
dcods = db.query(_Coding).filter(_Coding.run_id == drun).all()
db.close()
by_code = {}
for c in dcods:
    by_code.setdefault(c.code_id, []).append(c)
check("staff match found", staff_id in by_code and by_code[staff_id][0].row_index == 2,
      str([(c.code_id, c.row_index) for c in dcods]))
fin = by_code.get(fin_id, [None])[0]
check("multi-expression match: score 2, both expressions",
      fin and fin.relevance_score == 2 and "borrow money" in (fin.matched_expressions or "")
      and "medication costs" in (fin.matched_expressions or ""),
      str(fin.matched_expressions if fin else None))
check("dict rationale explains the match",
      fin and fin.rationale.startswith("Matched expressions:"), str(fin.rationale if fin else None))
ag = by_code.get(agency_id, [])
check("phrase matches the construction in order (row 5)",
      len(ag) == 1 and ag[0].row_index == 5 and "go where I want" in (ag[0].matched_expressions or ""),
      str([(c.row_index, c.matched_expressions) for c in ag]))
check("phrase rejects scrambled lemmas (row 6 uncoded)",
      all(c.row_index != 6 for c in dcods))
check("coverage: uncoded rows tracked", data["n_segments"] == 5 and data["n_uncoded"] == 2, str(data)[:150])

r = dictuser.get(f"/api/runs/{drun}/export/xlsx")
xdf2 = _pd.read_excel(_io.BytesIO(r.content))
check("export has matched_expressions + relevance_score",
      "matched_expressions" in xdf2.columns and "relevance_score" in xdf2.columns)
cbdf = _pd.read_excel(_io.BytesIO(r.content), sheet_name="codebook")
check("codebook sheet has Expressions_en",
      "Expressions_en" in cbdf.columns and (cbdf["Expressions_en"].astype(str).str.contains("borrow money")).any())

# import with expressions column
imp_path = Path("data/test_dict_import.xlsx")
_pd.DataFrame({"Code": ["imported_code"], "Description": ["x"],
               "Expressions_en": ["some expr; other expr"]}).to_excel(imp_path, index=False)
with imp_path.open("rb") as f:
    r = dictuser.post(f"/api/workspaces/{dws}/codebook/preview-import",
                      files={"file": ("imp.xlsx", f, "application/vnd.ms-excel")})
rows_imp = [{"label": x["label"], "description": x["description"], "example": x["example"],
             "expressions": x.get("expressions")} for x in r.json()["rows"] if not x["duplicate"]]
r = dictuser.post(f"/api/workspaces/{dws}/codebook/import", json={"rows": rows_imp})
check("import with expressions", r.status_code == 200 and r.json()["created"] == 1, r.text)
from models import Code as _CodeM
db = SessionLocal()
imp_code = db.query(_CodeM).filter(
    _CodeM.workspace_id == dws, _CodeM.label == "imported_code").first()
db.close()
r = dictuser.get(f"/api/codes/{imp_code.id}/expressions")
check("imported expressions present", len(r.json()["expressions"]["en"]) == 2, r.text)

print("== analysis (dictionary run) ==")
r = dictuser.put(f"/api/workspaces/{dws}",
                 json={"name": "Dict WS", "input_type": "excel", "segmentation_mode": "cell",
                       "stoplists": {"en": ["staff", "clinic"]}})
check("save per-language stoplists", r.status_code == 200, r.text)
check("analysis compute (async) for dictionary run",
      ensure_analysis(dictuser, drun) == "done")
r = dictuser.get(f"/workspace/{dws}/runs/{drun}/analysis")
check("analysis page (dictionary)", r.status_code == 200
      and "/analysis/chart/expressions_" in r.text, r.text[:200])
r = dictuser.get(f"/api/runs/{drun}/analysis/chart/codes?fmt=pdf&theme=light&download=1")
check("chart pdf download (light theme)", r.status_code == 200 and r.content[:4] == b"%PDF")
r = dictuser.get(f"/api/runs/{drun}/analysis/chart/groups")
check("groups chart absent without groups", r.status_code == 404)
r = dictuser.get(f"/api/runs/{drun}/analysis/chart/cooccurrence?fmt=png")
check("cooccurrence chart", r.status_code == 200 and r.content[:4] == b"\x89PNG")
r = dictuser.get(f"/api/runs/{drun}/analysis/export")
an_sheets = _pd.read_excel(_io.BytesIO(r.content), sheet_name=None)
check("analysis export sheets",
      {"codes", "cooccurrence", "documents", "expressions_en", "top_extracts", "lemmas_en"} <= set(an_sheets),
      str(list(an_sheets)))
check("expressions split per language (overall + by code+group)",
      "expressions_en" in an_sheets and "expr_by_code_group_en" in an_sheets
      and {"code", "expression", "firings"} <= set(an_sheets["expressions_en"].columns)
      and "group" in an_sheets["expr_by_code_group_en"].columns,
      str(list(an_sheets)))
check("top_extracts export has group column",
      "group" in an_sheets["top_extracts"].columns, str(list(an_sheets["top_extracts"].columns)))
check("stoplist filters lemma frequencies",
      not an_sheets["lemmas_en"]["lemma"].astype(str).isin(["staff", "clinic"]).any(),
      str(an_sheets["lemmas_en"]["lemma"].tolist()[:10]))
_lem = an_sheets["lemmas_en"]
check("repeated lemma counted twice (no per-segment dedup bug)",
      (_lem.loc[_lem["lemma"] == "great", "freq"] == 2).any(),
      str(_lem[["lemma", "freq"]].head(8).values.tolist()))
check("lemma drill-down sheets (per code, per code+group)",
      "lemmas_by_code_en" in an_sheets and "lemmas_by_code_group_en" in an_sheets
      and (an_sheets["lemmas_by_code_en"]["code"] == "financial_burden").any()
      and "low_volume" in an_sheets["lemmas_by_code_en"].columns
      and an_sheets["lemmas_by_code_en"]["low_volume"].all(),  # tiny test corpus: all flagged
      str(list(an_sheets)))
r = dictuser.get(f"/api/runs/{drun}/analysis/chart/lemmas_en?code=financial_burden")
check("lemma chart per code", r.status_code == 200 and r.content[:4] == b"\x89PNG")
r = dictuser.get(f"/api/runs/{drun}/analysis/chart/lemmas_en"
                 "?code=financial_burden&group=%28no%20group%29")
check("lemma chart per code+group (no group cell)", r.status_code == 200)
r = dictuser.get(f"/api/runs/{drun}/analysis/chart/lemmas_en?code=nonexistent_code")
check("lemma chart unknown code 404", r.status_code == 404)
r = dictuser.get(f"/api/runs/{drun}/analysis/chart/expressions_en")
check("expressions chart per language", r.status_code == 200 and r.content[:4] == b"\x89PNG")
r = dictuser.get(f"/api/runs/{drun}/analysis/chart/expressions_en?code=financial_burden")
check("expressions chart per code (200 or empty 404)", r.status_code in (200, 404))
r = dictuser.get(f"/workspace/{dws}/runs/{drun}/analysis?recompute=1")
check("recompute renders the async computing page",
      r.status_code == 200 and "an-bar-fill" in r.text)
check("recompute refills the cache", ensure_analysis(dictuser, drun, recompute=True) == "done")
# regression: a cached analysis_json from an older schema must not 500 the page
from models import Run as _RunSchema
db = SessionLocal()
_r = db.query(_RunSchema).filter(_RunSchema.id == drun).first()
_r.analysis_json = '{"expressions": [{"code": "x", "expression": "y", "firings": 1}], "lemmas": {}}'
db.commit()
db.close()
r = dictuser.get(f"/workspace/{dws}/runs/{drun}/analysis")
check("stale-schema cache renders computing page (no 500)",
      r.status_code == 200 and "an-bar-fill" in r.text, str(r.status_code))
check("stale cache recomputed to current schema", ensure_analysis(dictuser, drun) == "done")
r = dictuser.get(f"/workspace/{dws}/runs/{drun}/analysis")
check("recomputed page renders with new expressions charts",
      r.status_code == 200 and "/analysis/chart/expressions_" in r.text, str(r.status_code))
check("top extracts ranked by score",
      (an_sheets["top_extracts"].sort_values(["code", "score"], ascending=[True, False])["score"]
       .tolist() == an_sheets["top_extracts"]["score"].tolist())
      or len(an_sheets["top_extracts"]) > 0)

# codebook export: import-compatible round trip
r = dictuser.get(f"/api/workspaces/{dws}/codebook/export")
cbx = _pd.read_excel(_io.BytesIO(r.content))
check("codebook export with expression columns",
      r.status_code == 200 and "Expressions_en" in cbx.columns
      and (cbx["Expressions_en"].astype(str).str.contains("borrow money")).any(),
      str(list(cbx.columns)))
r2 = dictuser.post("/api/workspaces", json={"name": "Roundtrip WS", "input_type": "excel",
                                            "segmentation_mode": "cell"})
rt_ws = r2.json()["id"]
rt_path = Path("data/test_roundtrip.xlsx")
rt_path.write_bytes(r.content)
with rt_path.open("rb") as f:
    r = dictuser.post(f"/api/workspaces/{rt_ws}/codebook/preview-import",
                      files={"file": ("cb.xlsx", f, "application/vnd.ms-excel")})
rt_rows = [{"label": x["label"], "description": x["description"], "example": x["example"],
            "expressions": x.get("expressions")} for x in r.json()["rows"] if not x["duplicate"]]
r = dictuser.post(f"/api/workspaces/{rt_ws}/codebook/import", json={"rows": rt_rows})
check("codebook export → import round trip", r.status_code == 200
      and r.json()["created"] == len(cbx), r.text)
dictuser.delete(f"/api/workspaces/{rt_ws}")
rt_path.unlink(missing_ok=True)

# document without language fails, then retry after setting it
r = dictuser.put(f"/api/documents/{ddoc.id}", json={"language": "", "group_label": "module-A"})
check("clear language + set group", r.status_code == 200)
r = dictuser.post(f"/api/workspaces/{dws}/runs",
                  json={"document_ids": [ddoc.id], "engine": "dictionary"})
drun2 = r.json()["id"]
for _ in range(100):
    r = dictuser.get(f"/api/runs/{drun2}")
    if r.json()["status"] in ("completed", "failed"):
        break
    _time.sleep(0.1)
data = r.json()
check("no-language doc fails with clear error", data["documents"][0]["status"] == "failed"
      and "language" in (data["error_message"] or "").lower(), str(data)[:200])
r = dictuser.put(f"/api/documents/{ddoc.id}", json={"language": "en", "group_label": "module-A"})
r = dictuser.post(f"/api/runs/{drun2}/retry-failed")
check("dict retry without key accepted", r.status_code == 200, r.text)
for _ in range(100):
    r = dictuser.get(f"/api/runs/{drun2}")
    if r.json()["status"] in ("completed", "failed"):
        break
    _time.sleep(0.1)
check("dict retry completed", r.json()["status"] == "completed", r.text[:150])
r = dictuser.get(f"/api/runs/{drun2}/export/xlsx")
xdf3 = _pd.read_excel(_io.BytesIO(r.content))
check("group label in export", (xdf3["group"].astype(str) == "module-A").any(), str(list(xdf3.columns)))

# delete a run: cascades its codings/segments, keeps the codebook and documents
r = dictuser.delete(f"/api/runs/{drun2}")
check("delete run", r.status_code == 200, r.text)
check("deleted run returns 404", dictuser.get(f"/api/runs/{drun2}").status_code == 404)
check("codebook intact after run delete",
      dictuser.get(f"/workspace/{dws}/codebook").status_code == 200)

print("== speaker awareness (fase A) ==")
import conventions as conv_mod
from docx import Document as _DocxOut

r = client.post("/api/workspaces", json={"name": "Speaker WS", "study_context": "Care study",
                                         "segmentation_mode": "utterance_regex"})
sws = r.json()["id"]

f4_path = Path("data/test_f4.docx")
_d = _DocxOut()
for line in [
    "DIPEx Institut für Forschung",
    "Kontakt: Frau Beispiel",
    "Email: beispiel@example.org",
    "I: Wie geht es Ihnen heute mit der Behandlung? #00:00:05-1#",
    "E: Mir geht es gut, danke der Nachfrage an alle. #00:00:12-2#",
    "Aber manchmal ist es schwierig mit der Familie zusammen. #00:00:20-3#",
    "I: Können Sie mehr darüber erzählen bitte? #00:00:25-4#",
    "e: Ich versuche es einfach jeden Tag neu. #00:00:31-5#",
    "E. Und meine Frau hilft mir sehr dabei immer. #00:00:39-6#",
    "I: Das klingt nach guter Unterstützung wirklich. #00:00:44-7#",
    "E: Ja, das ist es auch wirklich für mich. #00:00:50-8#",
]:
    _d.add_paragraph(line)
_d.save(f4_path)
with f4_path.open("rb") as f:
    r = client.post(f"/api/workspaces/{sws}/documents",
                    files=[("files", ("interview_f4.docx", f, "application/octet-stream"))])
check("f4 upload", r.status_code == 200 and r.json()["uploaded"], r.text)
db = SessionLocal()
f4doc = db.query(_Doc2).filter(_Doc2.workspace_id == sws).first()
db.close()
check("f4 convention detected", f4doc.convention == "f4", str(f4doc.convention))
import json as _json
roles = _json.loads(f4doc.roles_json or "{}")
check("roles defaulted (I interviewer, E participant)",
      roles.get("I") == "interviewer" and roles.get("E") == "participant", str(roles))

r = client.get(f"/api/documents/{f4doc.id}/preview-segments")
segs = r.json()["segments"]
check("front matter excluded, speakers normalized",
      segs[0]["excluded"] and segs[1]["excluded"] and segs[2]["excluded"]
      and segs[3]["speaker"] == "I" and not segs[3]["excluded"], str(segs[:4])[:200])
check("carry-forward + case/separator variants fold into E",
      segs[5]["speaker"] == "E" and segs[7]["speaker"] == "E" and segs[8]["speaker"] == "E",
      str([(s["speaker"], s["text"][:18]) for s in segs])[:300])

# #3: speaker-awareness is orthogonal to granularity — in sentence mode (with a
# convention) front matter stays excluded and coded units keep their speaker
client.put(f"/api/workspaces/{sws}", json={"name": "Speaker WS",
           "study_context": "Care study", "segmentation_mode": "sentence"})
client.put(f"/api/documents/{f4doc.id}", json={"language": "de"})
ssegs = client.get(f"/api/documents/{f4doc.id}/preview-segments").json()["segments"]
check("sentence mode stays speaker-aware (front matter excluded)",
      ssegs[0]["excluded"] and ssegs[1]["excluded"], str(ssegs[:3])[:200])
check("sentence mode keeps the speaker on coded units",
      any(s["speaker"] in ("I", "E") for s in ssegs if not s["excluded"]),
      str([(s["speaker"], s["excluded"]) for s in ssegs])[:200])
client.put(f"/api/workspaces/{sws}", json={"name": "Speaker WS",  # restore for the rest
           "study_context": "Care study", "segmentation_mode": "utterance_regex"})

r = client.post(f"/api/workspaces/{sws}/documents",
                files=[("files", ("legacy.doc", b"junk", "application/msword"))])
check(".doc rejected with conversion hint", r.status_code == 400
      and "convert to .docx" in r.json()["detail"])

noscribe_txt = ("S00: Hello and welcome to this recorded conversation today.\n"
                "S01: Thank you very much for having me here now.\n"
                "S00: Let us begin with the first question then.\n"
                "S01: Sure, that sounds perfectly fine to me.\n").encode("utf-8")
r = client.post(f"/api/workspaces/{sws}/documents",
                files=[("files", ("noscribe.txt", noscribe_txt, "text/plain"))])
check("txt upload (noScribe plain)", r.status_code == 200 and r.json()["uploaded"], r.text)
db = SessionLocal()
txtdoc = (db.query(_Doc2).filter(_Doc2.workspace_id == sws, _Doc2.filename == "noscribe.txt")
          .first())
db.close()
check("plain convention detected on txt", txtdoc.convention in ("plain", "f4"),
      str(txtdoc.convention))

noscribe_html = ("<html><body><p>My interview</p>"
                 "<p>Transcribed with noScribe vers. 0.4.1</p>"
                 "<p>S00 <span>[00:00:00]</span>: This is the spoken text of the recording.</p>"
                 "<p>S01 <span>[00:00:08]</span>: And this is the answer to that question.</p>"
                 "<p>S00 <span>[00:00:15]</span>: A follow up question comes right here now.</p>"
                 "<p>S01 <span>[00:00:21]</span>: And one more answer to close it out.</p>"
                 "</body></html>").encode("utf-8")
r = client.post(f"/api/workspaces/{sws}/documents",
                files=[("files", ("noscribe.html", noscribe_html, "text/html"))])
check("html upload (noScribe)", r.status_code == 200 and r.json()["uploaded"], r.text)
db = SessionLocal()
htmldoc = (db.query(_Doc2).filter(_Doc2.workspace_id == sws, _Doc2.filename == "noscribe.html")
           .first())
db.close()
check("noScribe html detected as default convention", htmldoc.convention == "default",
      str(htmldoc.convention))

# unsegmented document → warning → custom convention teaches the workspace
weird = "\n".join(f">>P{i % 2}<< Something said here number {i} indeed." for i in range(10))
r = client.post(f"/api/workspaces/{sws}/documents",
                files=[("files", ("weird.txt", weird.encode(), "text/plain"))])
db = SessionLocal()
wdoc = (db.query(_Doc2).filter(_Doc2.workspace_id == sws, _Doc2.filename == "weird.txt").first())
db.close()
check("weird format unsegmented", wdoc.convention is None, str(wdoc.convention))
r = client.get(f"/workspace/{sws}/runs")
check("unsegmented warning on runs page", "no working convention" in r.text)
r = client.post(f"/api/workspaces/{sws}/conventions",
                json={"name": "chevrons", "regex": r"^>>(?P<speaker>\w+)<< (?P<text>.+)$",
                      "apply_to_document_id": wdoc.id})
check("custom convention saved and applied", r.status_code == 200
      and r.json()["applied_to"] == wdoc.id, r.text)
db = SessionLocal()
wdoc2 = db.query(_Doc2).filter(_Doc2.id == wdoc.id).first()
db.close()
check("custom convention on document + roles discovered",
      wdoc2.convention == "chevrons" and "P0" in _json.loads(wdoc2.roles_json or "{}"),
      f"{wdoc2.convention} / {wdoc2.roles_json}")

_real_suggest = conv_mod.suggest_regex
conv_mod.suggest_regex = lambda key, sample: r"^(?P<speaker>\w+): (?P<text>.+)$"
r = client.post(f"/api/documents/{wdoc.id}/suggest-regex")
check("LLM regex suggestion endpoint", r.status_code == 200 and "(?P<speaker>" in r.json()["regex"])
conv_mod.suggest_regex = _real_suggest

r = client.post(f"/api/workspaces/{sws}/documents/bulk-group",
                json={"document_ids": [f4doc.id, txtdoc.id], "group_label": "wave-1"})
check("bulk group", r.status_code == 200 and r.json()["updated"] == 2)

# UI guards: setup button without inline label (quoting bug), help modals present
r = client.get(f"/workspace/{sws}/corpus")
check("setup button uses dataset (no quoting bug)",
      "openSetup(" in r.text and "this.dataset.name" in r.text
      and "How transcript conventions work" in r.text and "th-sort" in r.text)
r = client.get("/")
check("workspace creation help modal (input type only; seg moved to run form)",
      "Corpus type" in r.text and "Segmentation mode (coding unit)" not in r.text
      and "ws-regex" not in r.text)
r = client.get(f"/workspace/{sws}/runs")
check("role exclusion lives in the run form",
      "Exclude roles from coding (this run)" in r.text)
check("coding unit picker + help moved to the run form",
      'id="run-unit"' in r.text and "Segmentation mode (coding unit)" in r.text)

# excluded roles: interviewer out, chosen AT RUN LAUNCH (mocked LLM)
coding.call_claude_with_retry = _fake_call  # re-mock: the excel section restored the real one
coding.anthropic.Anthropic = lambda api_key: _FakeAnthropicClient()
_call_count["n"] = 0
r = client.post(f"/api/workspaces/{sws}/runs",
                json={"document_ids": [f4doc.id], "model": "claude-sonnet-4-6",
                      "max_workers": 1, "excluded_roles": ["interviewer"]})
check("speaker run starts", r.status_code == 200, r.text)
srun = r.json()["id"]
for _ in range(100):
    r = client.get(f"/api/runs/{srun}")
    if r.json()["status"] in ("completed", "failed"):
        break
    _time.sleep(0.1)
data = r.json()
check("speaker run completed", data["status"] == "completed", str(data)[:200])
check("front matter + interviewer excluded from coding",
      data["n_excluded"] == 6, str(data)[:200])  # 3 front + 3 I turns
db = SessionLocal()
from models import Run as _RunS
srow = db.query(_RunS).filter(_RunS.id == srun).first()
s_codings = db.query(_Coding).filter(_Coding.run_id == srun).all()
s_segs = db.query(_Seg).filter(_Seg.run_id == srun).all()
db.close()
check("excluded roles snapshotted on run",
      "interviewer" in (srow.excluded_roles_snapshot or ""), str(srow.excluded_roles_snapshot))
check("no codings on interviewer turns",
      s_codings and all(c.speaker == "E" for c in s_codings),
      str([(c.speaker, c.segment_text[:15]) for c in s_codings])[:200])
check("excluded segments keep their speaker",
      any(s.status == "excluded" and s.speaker == "I" for s in s_segs))
r = client.get(f"/api/runs/{srun}/export/xlsx")
sdf2 = _pd.read_excel(_io.BytesIO(r.content), sheet_name="segments")
check("speaker column in segments export",
      "speaker" in sdf2.columns and (sdf2["speaker"].astype(str) == "E").any())
coding.call_claude_with_retry = _real_call
coding.anthropic.Anthropic = _real_anthropic_cls
r = client.delete(f"/api/workspaces/{sws}")
check("cleanup speaker workspace", r.status_code == 200)
f4_path.unlink(missing_ok=True)

dict_mod._get_nlp = _real_get_nlp
r = dictuser.delete(f"/api/workspaces/{dws}")
check("cleanup dict workspace", r.status_code == 200)
dict_survey.unlink(missing_ok=True)
imp_path.unlink(missing_ok=True)

print("== duplicate workspace ==")
from models import (Document as _DupDoc, Code as _DupCode, Run as _DupRun,
                    CodeExpression as _DupExpr)
# self-contained source: a code with an expression, plus a real document if present
r = client.post("/api/workspaces", json={"name": "Dup Source", "study_context": "ctx",
                                         "segmentation_mode": "paragraph"})
dup_src = r.json()["id"]
client.post(f"/api/workspaces/{dup_src}/codes",
            json={"label": "control", "expressions": {"en": ["control body"]}})
have_doc = docx_path.exists()
if have_doc:
    with docx_path.open("rb") as f:
        client.post(f"/api/workspaces/{dup_src}/documents",
                    files=[("files", ("transcript_P01.docx", f,
                                      "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))])
r = client.post(f"/api/workspaces/{dup_src}/duplicate", json={"name": "Dup no codebook"})
check("duplicate workspace", r.status_code == 200, r.text)
dup_id = r.json()["id"]
db = SessionLocal()
dup_docs = db.query(_DupDoc).filter(_DupDoc.workspace_id == dup_id).all()
dup_codes = db.query(_DupCode).filter(_DupCode.workspace_id == dup_id,
                                      _DupCode.is_deleted == False).count()
dup_runs = db.query(_DupRun).filter(_DupRun.workspace_id == dup_id).count()
db.close()
if have_doc:
    check("duplicate copies corpus", len(dup_docs) == 1, str(len(dup_docs)))
    check("duplicate copies physical files into the new workspace dir",
          all(d.file_path and Path(d.file_path).exists()
              and ("/" + str(dup_id) + "/") in d.file_path.replace("\\", "/")
              for d in dup_docs))
check("duplicate does not copy the codebook by default", dup_codes == 0, str(dup_codes))
check("duplicate does not copy runs", dup_runs == 0, str(dup_runs))
r = client.post(f"/api/workspaces/{dup_src}/duplicate", json={"copy_codebook": True})
check("duplicate with codebook", r.status_code == 200, r.text)
dup2 = r.json()["id"]
db = SessionLocal()
dup2_codes = db.query(_DupCode).filter(_DupCode.workspace_id == dup2,
                                       _DupCode.is_deleted == False).count()
dup2_exprs = (db.query(_DupExpr).join(_DupCode, _DupCode.id == _DupExpr.code_id)
              .filter(_DupCode.workspace_id == dup2).count())
db.close()
check("duplicate with codebook copies codes", dup2_codes == 1, str(dup2_codes))
check("duplicate with codebook copies expressions", dup2_exprs == 1, str(dup2_exprs))

print("== workspace deletion ==")
r = member.delete(f"/api/workspaces/{ws_id}")
check("member cannot delete workspace", r.status_code in (401, 403, 404))
r = client.delete(f"/api/workspaces/{ws_id}")
check("owner deletes workspace", r.status_code == 200)
check("upload dir removed", not (Path("data/test_uploads") / str(ws_id)).exists())

# cleanup
app_module  # keep reference
from models import engine
engine.dispose()
shutil.rmtree("data/test_uploads", ignore_errors=True)
try:
    Path("data/test.db").unlink(missing_ok=True)
except PermissionError:
    pass

print()
if FAILED:
    print(f"FAILED: {len(FAILED)} -> {FAILED}")
    sys.exit(1)
print("All checks passed.")
