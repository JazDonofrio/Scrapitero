"""Conexión a PostgreSQL+PostGIS via SQLAlchemy."""

import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from loguru import logger


def get_database_url() -> str:
    return (
        f"postgresql+psycopg://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
        f"@{os.environ.get('DB_HOST', 'localhost')}:{os.environ.get('DB_PORT', '5432')}"
        f"/{os.environ.get('DB_NAME', 'scrapitero')}"
    )


def get_engine():
    url = get_database_url()
    engine = create_engine(url, echo=False, pool_pre_ping=True)
    return engine


def get_session() -> Session:
    engine = get_engine()
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal()


def check_connection() -> bool:
    """Verifica que la DB esté levantada y PostGIS activo."""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            version = conn.execute(text("SELECT PostGIS_Version()")).scalar()
            logger.info(f"PostGIS OK: {version}")
            return True
    except Exception as e:
        logger.error(f"DB connection failed: {e}")
        return False
