
import os
from sqlalchemy import select
from main import SessionLocal, User, Role, password_hasher

email = os.environ["ADMIN_RESET_EMAIL"].strip().lower()
password = os.environ["ADMIN_RESET_PASSWORD"]

if "@" not in email or len(password) < 12:
    raise SystemExit("Invalid admin credentials")

with SessionLocal() as db:
    admins = db.scalars(
        select(User).where(User.role == Role.admin.value)
    ).all()

    if len(admins) != 1:
        raise SystemExit(
            f"Expected one admin, found {len(admins)}"
        )

    existing = db.scalar(
        select(User).where(User.email == email)
    )

    if existing and existing.id != admins[0].id:
        raise SystemExit("Email already in use")

    admins[0].email = email
    admins[0].password_hash = password_hasher.hash(password)
    db.commit()

print("ADMIN RESET SUCCESSFUL")
