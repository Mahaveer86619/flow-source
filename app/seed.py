from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import Role, User
from .services import auth_service


def seed_roles(db: Session):
    roles = ["admin", "user"]
    for role_name in roles:
        role = db.query(Role).filter(Role.name == role_name).first()
        if not role:
            db.add(Role(name=role_name))
    db.commit()
    print("Roles seeded.")


def seed_admin(db: Session):
    admin_role = db.query(Role).filter(Role.name == "admin").first()
    if not admin_role:
        print("Admin role not found. Seed roles first.")
        return

    admin_user = db.query(User).filter(User.username == "admin").first()
    if not admin_user:
        hashed_password = auth_service.get_password_hash("admin123")
        db.add(
            User(
                username="admin",
                email="admin@example.com",
                hashed_password=hashed_password,
                role_id=admin_role.id,
                is_active=True,
            )
        )
        db.commit()
        print("Admin user seeded (admin/admin123).")
    else:
        print("Admin user already exists.")


if __name__ == "__main__":
    db = SessionLocal()
    try:
        seed_roles(db)
        seed_admin(db)
    finally:
        db.close()
