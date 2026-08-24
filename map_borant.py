"""Link existing AutoCode users to the subjects an SSO gate knows them by.

Run once, by hand, BEFORE switching AUTH_MODE to `gateway`, and read the report
before believing it:

    docker exec -w /app autocode python map_borant.py --report
    docker exec -w /app autocode python map_borant.py \
        --map you@example.org=01ABC...

Why a script and not an automatic match at request time: linking by email is
defensible in principle, because the address arrives from the gate and not from
the client — but doing it live means one typo in the gate's admin panel
silently hands one person another person's runs, and nobody finds out. A script
gets read before it is run, and prints what it did.

What it deliberately does NOT do:

  * It never overwrites an existing link. A conflict is reported; --unlink
    undoes one on purpose.
  * It never touches password_hash. Local passwords stay populated in gateway
    mode, because that is what makes AUTH_MODE=local a way back — and a user
    who only ever arrived through the gate has no password to come back with.
  * It never changes is_admin nor the per-user Anthropic key. Promotion stays a human decision in /admin.
"""
import argparse
import sys

from models import SessionLocal, User, Workspace


def report(db) -> None:
    rows = db.query(User).order_by(User.email).all()
    legati = [u for u in rows if u.borant_sub]
    scoperti = [u for u in rows if not u.borant_sub]

    print(f"\n{len(rows)} utenti, {len(legati)} legati, {len(scoperti)} scoperti.\n")
    if legati:
        print("LEGATI - entrano dal gate:")
        for u in legati:
            n = db.query(Workspace).filter(Workspace.owner_id == u.id).count()
            print(f"  {u.email:<32} {'admin' if u.is_admin else '     '}  {n:>2} ws  {u.borant_sub}")
    if scoperti:
        print("\nSCOPERTI - in gateway NON entrano finche' non hanno un subject:")
        for u in scoperti:
            n = db.query(Workspace).filter(Workspace.owner_id == u.id).count()
            flag = "" if u.is_active else "  (disattivato)"
            print(f"  {u.email:<32} {'admin' if u.is_admin else '     '}  {n:>2} ws{flag}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", action="append", default=[], metavar="EMAIL=SUBJECT",
                    help="lega un utente a un subject del gate; ripetibile")
    ap.add_argument("--unlink", action="append", default=[], metavar="EMAIL",
                    help="toglie il legame di un utente; ripetibile")
    ap.add_argument("--report", action="store_true",
                    help="stampa chi e' legato e chi no, e non cambia niente")
    args = ap.parse_args()

    db = SessionLocal()
    cambiate = 0

    for coppia in args.map:
        email, sep, subject = coppia.partition("=")
        email, subject = email.strip().lower(), subject.strip()
        if not sep or not email or not subject:
            print(f"  SALTO     {coppia!r}: serve la forma email=subject")
            continue
        u = db.query(User).filter(User.email == email).first()
        if u is None:
            print(f"  ASSENTE   {email}: nessun utente con questo indirizzo")
            continue
        if u.borant_sub == subject:
            print(f"  GIA-OK    {email} -> {subject}")
            continue
        if u.borant_sub:
            print(f"  CONFLITTO {email}: gia' legato a {u.borant_sub}, non sovrascrivo. "
                  f"Usa --unlink prima, se e' voluto.")
            continue
        altro = db.query(User).filter(User.borant_sub == subject).first()
        if altro is not None:
            print(f"  CONFLITTO {subject}: gia' usato da {altro.email}, non tocco niente.")
            continue
        u.borant_sub = subject
        db.commit()
        print(f"  LEGATO    {email} -> {subject}")
        cambiate += 1

    for email in args.unlink:
        email = email.strip().lower()
        u = db.query(User).filter(User.email == email).first()
        if u is None:
            print(f"  ASSENTE   {email}: nessun utente con questo indirizzo")
            continue
        if not u.borant_sub:
            print(f"  GIA-OK    {email}: non era legato a niente")
            continue
        vecchio = u.borant_sub
        u.borant_sub = None
        db.commit()
        print(f"  SLEGATO   {email} (era {vecchio})")
        cambiate += 1

    if args.map or args.unlink:
        print(f"\n{cambiate} righe cambiate.")
    report(db)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
