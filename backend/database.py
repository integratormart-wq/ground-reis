from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from . import models

engine = models.engine
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
