"""Interactive one-time admin creation: python create_admin.py"""
from getpass import getpass
from sqlalchemy import select
from main import Base, SessionLocal, User, Role, engine, password_hasher

Base.metadata.create_all(engine)
email = input("Admin email: ").strip().lower()
name = input("Display name: ").strip()
password = getpass("Admin password (12+ characters): ")
if not name or "@" not in email or len(password) < 12:
    raise SystemExit("Enter valid details and a password of at least 12 characters.")
with SessionLocal() as db:
    if db.scalar(select(User.id).where(User.email == email)):
        raise SystemExit("Account already exists. Choose another email.")
    db.add(User(email=email, full_name=name, password_hash=password_hasher.hash(password),
                role=Role.admin.value, is_verified=True))
    db.commit()
print("Admin created. You can now approve volunteer accounts.")
