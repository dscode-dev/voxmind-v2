from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from settings import settings


engine = create_engine(
    settings.database_url,
    pool_size=settings.database_pool_size,
    max_overflow=settings.database_max_overflow,
    pool_pre_ping=True
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False
)


def get_db():

    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()