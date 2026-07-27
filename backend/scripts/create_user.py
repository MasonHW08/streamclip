import argparse

from app.core.db import SessionLocal
from app.models.user import User
from app.services.password import hash_password


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an internal StreamClip Co. user")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--role", default="admin")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        user = User(email=args.email, hashed_password=hash_password(args.password), role=args.role)
        db.add(user)
        db.commit()
        print(f"Created user {user.email} (id={user.id}, role={user.role})")
    finally:
        db.close()


if __name__ == "__main__":
    main()
