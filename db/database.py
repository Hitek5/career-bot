from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import DB_PATH
from db.models import Base

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
SessionLocal = sessionmaker(bind=engine)


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)


def get_session():
    return SessionLocal()
