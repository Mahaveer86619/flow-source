import argparse
import sys

from app.database import Base, SessionLocal, engine
from app.seed import seed_admin, seed_roles


def create_db():
    print("Creating database tables...")
    Base.metadata.create_all(bind=engine)
    print("Tables created.")


def seed_db():
    print("Seeding database...")
    db = SessionLocal()
    try:
        seed_roles(db)
        seed_admin(db)
    finally:
        db.close()
    print("Seeding complete.")


def drop_db():
    print("Dropping database tables...")
    Base.metadata.drop_all(bind=engine)
    print("Tables dropped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manage the Flow Music API database.")
    parser.add_argument(
        "command", choices=["create", "seed", "drop", "reset"], help="Command to run."
    )

    args = parser.parse_args()

    if args.command == "create":
        create_db()
    elif args.command == "seed":
        seed_db()
    elif args.command == "drop":
        drop_db()
    elif args.command == "reset":
        drop_db()
        create_db()
        seed_db()
