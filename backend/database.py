from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from backend.models import Base, engine

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
