from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User
from app.schemas import UserCreate
from app.time_utils import validate_timezone
from app.core.auth import generate_access_token, hash_access_token


class UserService:
    def __init__(self, db: Session):
        self.db = db

    def create(self, data: UserCreate) -> User:
        user, _ = self.create_with_token(data)
        return user

    def create_with_token(self, data: UserCreate) -> tuple[User, str]:
        validate_timezone(data.timezone)
        access_token = generate_access_token()
        user = User(name=data.name.strip(), timezone=data.timezone, status=data.status)
        user.auth_token_hash = hash_access_token(access_token)
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user, access_token

    def get(self, user_id):
        user = self.db.scalar(select(User).where(User.id == user_id))
        if user is None:
            raise LookupError("user not found")
        return user

