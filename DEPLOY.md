# Deploying Autocode

Autocode is a FastAPI app backed by one SQLite file, with a background thread for coding
runs. No external services required beyond the Claude API (only needed for the LLM engine —
the dictionary engine works offline).

## 1. Configuration (environment variables)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SECRET_KEY` | **yes, in production** | `change-me-in-production` | signs JWTs — set a long random value |
| `FERNET_KEY` | **yes, in production** | `change-me-in-production` | encrypts per-user Anthropic API keys and TOTP secrets at rest |
| `DATABASE_URL` | no | `sqlite:////app/data/autocode.db` | SQLite path |
| `UPLOAD_DIR` | no | `/app/data/uploads` | corpus file storage |

Generate the keys:

```bash
python -c "import secrets; print(secrets.token_hex(32))"                              # SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # FERNET_KEY
```

## 2. Local / bare-metal

```bash
pip install -r requirements.txt
python -m spacy download en_core_web_md de_core_news_md fr_core_news_md it_core_news_md
cp .env.example .env   # edit SECRET_KEY / FERNET_KEY
python seed_admin.py
uvicorn app:app --host 0.0.0.0 --port 8000
```

## 3. Docker

```bash
cp .env.example .env   # edit SECRET_KEY / FERNET_KEY
docker compose up -d --build
docker compose exec app python seed_admin.py
```

`docker-compose.yml` maps the app to `127.0.0.1:8007` and mounts `./data` for the SQLite
file and uploads. `mem_limit: 1500m` plus `OMP_NUM_THREADS=1` / `OPENBLAS_NUM_THREADS=1` cap
memory on small boxes — spaCy loads 4 language models and a large corpus can otherwise
OOM-kill the container on a 2-CPU/4GB VPS. Add host swap if running on similarly small
hardware.

## 4. Reverse proxy (HTTPS)

Example **Caddy**:

```
autocode.example.org {
    reverse_proxy 127.0.0.1:8007
}
```

Reload after editing: `systemctl reload caddy`.

## 5. Verify

- `https://autocode.example.org/login` — auth (2FA enrollment forced on first login)
- `https://autocode.example.org/` — workspace list

## 6. Updating

```bash
cd /opt/apps/autocode
git pull
docker compose up -d --build
```

`data/` (SQLite + uploads) and `.env` are gitignored — `git pull` never touches them.

## 7. Backups

```bash
cp data/autocode.db backup-$(date +%F).db
tar czf backup-uploads-$(date +%F).tar.gz data/uploads
```

SQLite is a single file — copying it (plus the uploads folder) is enough.
