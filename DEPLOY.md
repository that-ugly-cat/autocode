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
| `PUBLIC_URL` | **behind a proxy** | `http://localhost:8007` | public origin; the MCP transport refuses any other Host |

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

## The model surface (`/mcp`)

AutoCode speaks MCP at **`/mcp`**, so an assistant can list workspaces, read and
write the codebook, estimate and launch runs, and read back what was coded. Each
user mints their own key from **Profile → Model access**; clients that cannot set
headers can put it in the path instead (`/mcp/k/<key>/`).

**A key is an identity, not a capability.** It reaches exactly the workspaces its
owner is a member of — the same `workspace_for()` the web app uses — and it
carries no Anthropic credential of its own, so a run started from a chat spends
the owner's key and nobody else's. Revoking a key, or deactivating the person,
closes the surface with it.

**`PUBLIC_URL` is not optional behind a proxy.** The transport checks the Host
header against DNS rebinding, and refuses every name it was not told about; the
symptom is a tool that looks broken and is a missing variable.

**Keep `/mcp` outside any gate.** A model client has no browser and no cookie, so
putting it behind a domain session is a way of switching it off. `/mcp/*` covers
the `/mcp/k/<key>` form too.

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

Public surface to keep outside any gate: `/healthz`, `/guide`, `/static/*`,
**`/imgs/*`** and **`/mcp` + `/mcp/*`** — the third is a separate mount holding
the logo and favicon that `login.html` itself loads, so gating it would serve a
login page with no logo, and the last has its own per-user key.

```
autocode.example.org {
    @pubbliche path /healthz /guide /static/* /imgs/* /mcp /mcp/*
    handle @pubbliche {
        reverse_proxy 127.0.0.1:8007
    }
    handle {
        import borantid
        reverse_proxy 127.0.0.1:8007
    }
}
```

Rollback is two independent moves: `AUTH_MODE=local` plus
`docker compose up -d`, and dropping the gate's block from the reverse proxy.

## The landing, the home, and the role hint

Same shape in every app of the perimeter, so there is nothing to remember per
tool.

**`/` is a public showcase and never asks who is reading it.** Not laziness: on
the public branch of the reverse proxy the `X-Borant-*` headers are stripped by
construction, so a branch on the user is always false behind the gate and
sometimes true without one — the same page with two behaviours. By not asking,
the page is identical in both modes and one button covers all four cases:
gated or standalone, already signed in or not. It also shows no internal
counts: anyone can read it.

**The app lives at `/app`**, which is gated, and the showcase's button
points there — not at `/login`, which on a page that can never recognise anyone
would close a loop with no way in, and not at the gate's own URL, which would
work and would wire Borant ID into an app that must keep running without it.

**The role hint is honoured, and its vocabulary is one word: `admin`.** That
flag opens user management — deactivate, reset password and second factor — and
not the product, which anyone holding a grant already has. A profile created as
an admin this way is logged loudly. An unrecognised hint grants nothing.

**A page that needs an identity fails closed.** In `gateway` an unauthenticated
request does *not* redirect to `/login` — the app switches that route off in
this mode and sends it back, so the two would bounce forever. Production never
shows it because the gate intercepts first, but a wrong proxy matcher would
produce a spin instead of an error, and a loop is far harder to diagnose than a
status code. The answer is a 503 naming what the operator should check, because
a request arriving with no identity means the gate did not run.
