
import os
from sqlalchemy import select
from main import SessionLocal, User, Role, password_hasher

email = os.environ["ADMIN_RESET_EMAIL"].strip().lower()
password = os.environ["ADMIN_RESET_PASSWORD"]

if "@" not in email or len(password) < 12:
    raise SystemExit("Invalid admin credentials")

with SessionLocal() as db:
    existing = db.scalar(
        select(User).where(User.email == email)
    )

    if existing:
        raise SystemExit("Email already exists")

    admin = User(
        email=email,
        full_name="Administrator",
        password_hash=password_hasher.hash(password),
        role=Role.admin.value,
        is_verified=True
    )

    db.add(admin)
    db.commit()

print("ADMIN CREATED SUCCESSFULLY")
