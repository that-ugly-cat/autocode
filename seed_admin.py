"""Create the first admin user. Run once:
   python seed_admin.py <email> <name> <password>
"""
import sys

from auth import hash_password
from models import SessionLocal, User, init_db

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python seed_admin.py <email> <name> <password>")
        sys.exit(1)
    email, name, password = sys.argv[1].strip().lower(), sys.argv[2], sys.argv[3]
    init_db()
    db = SessionLocal()
    try:
        if db.query(User).filter(User.email == email).first():
            print(f"User {email} already exists")
            sys.exit(1)
        user = User(email=email, name=name, hashed_password=hash_password(password), is_admin=True)
        db.add(user)
        db.commit()
        print(f"Admin '{email}' created with id={user.id}")
    finally:
        db.close()
