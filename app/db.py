from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# sqlite needs check_same_thread off for the TestClient/uvicorn threads.
_is_sqlite = settings.database_url.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}
engine = create_engine(settings.database_url, connect_args=_connect_args)

if _is_sqlite:
    # WAL = concurrent readers during a write; busy_timeout = writers wait for the lock instead of
    # erroring "database is locked" under concurrent requests. Postgres (prod) needs none of this.
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _rec):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _migrate_sqlite() -> None:
    """Idempotent hand-migrations for the pre-existing SQLite DB (no Alembic yet): create_all
    won't add columns/indexes to a table that already exists, so a running company_os.db misses
    anything added after its tables were first created. ponytail: wire Alembic when this grows a
    third entry."""
    if not _is_sqlite:
        return  # Postgres (prod) builds the current schema fresh via create_all / real migrations
    from sqlalchemy import text
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(projects)")}
        if "leadforge_lead_id" not in cols:
            conn.exec_driver_sql("ALTER TABLE projects ADD COLUMN leadforge_lead_id VARCHAR")
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_projects_org_leadforge "
            "ON projects (org_id, leadforge_lead_id)"
        ))


def init_db() -> None:
    # ponytail: create_all for Phase 0; wire Alembic when the schema starts churning.
    from app import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=engine)
    _migrate_sqlite()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
