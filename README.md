<p align="center">
  <b>Collaborative qualitative coding, LLM-assisted or dictionary-based.</b><br>
  Multi-user workspaces, REFI-QDA export, speaker-aware transcript segmentation.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: AGPL v3" src="https://img.shields.io/badge/License-AGPLv3-blue.svg"></a>
</p>

---

Autocode is a self-hosted qualitative data analysis (QDA) tool. Teams upload interview
transcripts (DOCX) or survey free-text (Excel) into shared workspaces, build a codebook
together, and run automated coding — either LLM-based (Claude, with per-user API keys) or a
deterministic multilingual dictionary engine. Results export to Excel, `.qdc`, and `.qdpx`
(REFI-QDA, MAXQDA-compatible).

## Features

- **Two coding engines**: LLM (Claude, prompt caching, cost tracking per run) and a
  dictionary engine (spaCy lemmatization, bag/phrase expression matching, no API key needed).
- **Multi-user workspaces** with roles (admin / owner / member), shared codebook and corpus.
- **Speaker-aware transcripts**: auto-detected speaker conventions (regex presets + custom),
  per-document role mapping, carry-forward, front-matter auto-cut, per-role exclusion from
  coding.
- **Flexible segmentation**: document / paragraph / sentence (spaCy) / regex-utterance for
  DOCX; cell / sentence for Excel survey data.
- **Full run coverage**: every unit is recorded (`coded` / `no_code` / `error`, with
  rationale), not just positive matches.
- **Analysis page** per run: code frequencies, code×group normalization, co-occurrence
  matrix, lemma frequency drill-down, chart export (PNG/PDF).
- **Export**: Excel (codings + segments + codebook), `.qdc`, `.qdpx` (MAXQDA), corpus bundle
  (`.autocorpus`) for backup/transfer between workspaces.
- **Security**: JWT httpOnly cookie auth, bcrypt passwords, mandatory TOTP 2FA, Fernet-
  encrypted per-user Anthropic API keys.

## Quick start

```bash
git clone https://github.com/that-ugly-cat/autocode.git
cd autocode
pip install -r requirements.txt
python -m spacy download en_core_web_md   # + de/fr/it_core_news_md as needed
cp .env.example .env   # edit SECRET_KEY / FERNET_KEY
python seed_admin.py   # create the first admin user
uvicorn app:app --reload
```

Open http://localhost:8000/login. Non-admin users can self-register.

## Stack

FastAPI · SQLite (SQLAlchemy) · Jinja2 + vanilla JS (AJAX polling, no framework) · spaCy ·
Claude (Anthropic SDK) for the LLM engine.

```
app.py            — routes
auth.py           — JWT + bcrypt + TOTP (2FA)
coding.py         — LLM coding engine (background run thread)
dictionary.py     — dictionary coding engine (lemma matching)
conventions.py    — speaker convention detection/parsing
segmentation.py   — document/paragraph/sentence/utterance splitting
analysis.py       — per-run analysis (frequencies, co-occurrence, charts)
exports.py        — Excel / QDC / QDPX / corpus bundle export-import
models.py         — SQLAlchemy models
```

## Deployment

See **[DEPLOY.md](DEPLOY.md)** for environment variables, Docker setup, reverse proxy, and
backups.

## Tech notes

- Set `SECRET_KEY` and `FERNET_KEY` in production (insecure defaults for local dev).
- SQLite + uploaded files live under `data/` — back up by copying the folder.
- 2FA (TOTP) is mandatory for all users; the first admin is created via `seed_admin.py`, not
  through self-registration.

## License

Copyright (C) 2026 Giovanni Spitale. Licensed under AGPL-3.0 — fork it, host it, sell access
to it, but keep it closed-source and you're in violation. No SaaS forks that don't share
back. See [LICENSE](LICENSE).
