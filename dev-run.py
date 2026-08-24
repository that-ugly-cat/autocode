"""Avvio locale di AutoCode, per guardarlo in un browser.

DB usa-e-getta in `.devdata/`: `DATABASE_URL` e `UPLOAD_DIR` puntano lì, quindi
questo script non ha modo di toccare `data/autocode.db` né il corpus vero.
`SECRET_KEY` e `FERNET_KEY` sono generate al primo giro e tenute nella stessa
cartella, così i login locali sopravvivono a un riavvio.

`PUBLIC_URL` non è cosmetico: il trasporto MCP confronta l'Host con una lista, e
senza questa riga `/mcp` rifiuterebbe anche `localhost:8007`.

`os.chdir(BASE)` non è cosmetico nemmeno lui: `app.py` monta `static/`, `imgs/`
e `templates/` con percorsi relativi, quindi la working directory *deve* essere
quella dell'app, qualunque sia quella da cui si lancia.

È uno script Python e non uno shell script per la ragione di sempre: l'anteprima
lancia bash, che ragiona in `/mnt/c/...`, mentre l'interprete è un binario
Windows. Qui l'interprete è già quello giusto.
"""
import os
import pathlib
import secrets

BASE = pathlib.Path(__file__).resolve().parent
DEV = BASE / ".devdata"
DEV.mkdir(exist_ok=True)

chiave = DEV / "secret.key"
if not chiave.exists():
    chiave.write_text(secrets.token_urlsafe(48), encoding="utf-8")
fernet = DEV / "fernet.key"
if not fernet.exists():
    from cryptography.fernet import Fernet
    fernet.write_text(Fernet.generate_key().decode(), encoding="utf-8")

os.environ.setdefault("SECRET_KEY", chiave.read_text(encoding="utf-8").strip())
os.environ.setdefault("FERNET_KEY", fernet.read_text(encoding="utf-8").strip())
os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + str(DEV / "dev.db").replace("\\", "/"))
os.environ.setdefault("UPLOAD_DIR", str(DEV / "uploads"))
os.environ.setdefault("PUBLIC_URL", "http://localhost:8007")
os.environ.setdefault("AUTH_MODE", "local")

os.chdir(BASE)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8007)
