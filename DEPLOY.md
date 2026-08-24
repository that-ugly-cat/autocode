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
yourdomain.example {
    reverse_proxy 127.0.0.1:8007
}
```

Reload after editing: `systemctl reload caddy`.

## 5. Verify

- `https://yourdomain.example/login` — auth (2FA enrollment forced on first login)
- `https://yourdomain.example/` — workspace list

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

## Authentication: two modes

AutoCode authenticates on its own by default and needs no identity provider.
`AUTH_MODE=gateway` is a second mode, for a deployment behind an SSO gate that
speaks the `X-Borant-*` header contract.

```
AUTH_MODE=local     (default)   email + password + TOTP, as always
AUTH_MODE=gateway               the upstream gate vouches via X-Borant-Sub
```

**Read this before switching.** AutoCode's second factor is real: `POST
/api/auth/login` grants a ten-minute pending token and nothing else until TOTP
is passed. In `gateway` the local login is off, and the local second factor
goes with it — so the gate has to carry a `two_factor` policy or the migration
is a weakening rather than a move. The policy belongs on `/`, not on `/admin`:
a level goes on a class of secrets, and the per-user Anthropic keys are set
from `/profile`.

What else changes in `gateway`: local registration is closed (two parallel
identities otherwise), `/2fa` redirects, and logout returns a `redirect` in its
JSON so the browser goes on to the gate — dropping the local cookie alone is
not signing out while the gate still holds the session.

`BORANT_TRUSTED_PROXY` is measured, not deduced. Under Docker it is a bridge
gateway and **not** `127.0.0.1`:

```
docker compose logs --tail 20 autocode
# INFO:  172.x.0.1:54321 - "GET / HTTP/1.1" 200 OK
```

Linking existing accounts to gate subjects is a one-off manual step, before the
mode is flipped:

```
docker exec -w /app autocode python map_borant.py --report
docker exec -w /app autocode python map_borant.py --map you@example.org=01ABC...
```

Public surface to keep outside any gate: `/healthz`, `/guide`, `/static/*` and
**`/imgs/*`** — the second is a separate mount holding the logo and favicon
that `login.html` itself loads, so gating it would serve a login page with no
logo.

Rollback is two independent moves: `AUTH_MODE=local` plus
`docker compose up -d`, and dropping the gate's block from the reverse proxy.
