"""Offline test harness: run the whole pipeline on in-memory SQLite, no Docker/PG.

The models declare Postgres JSONB columns; a compile hook renders those as plain
JSON on SQLite so `create_all` works. Services grab `app.db.SessionLocal` at
call-time, so rebinding the sessionmaker to a SQLite engine redirects every DB
call (services + FastAPI `get_session`) at once. Network integrations are mocked
per-test — nothing here touches the box.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB

import app.db as db
from app.db import Base
# import models so their tables register on Base.metadata before create_all
import app.models.domain
import app.models.site
import app.models.offer
import app.models.monitoring
# reference the modules so their table-registration side effect (create_all needs
# every table, incl. index_history from publish.check_index) isn't seen as a dead import
_REGISTER_TABLES = (app.models.domain, app.models.site, app.models.offer, app.models.monitoring)


@compiles(JSONB, "sqlite")
def _jsonb_as_json(element, compiler, **kw):  # DDL only; bind/result still json.dumps/loads
    return "JSON"


@pytest.fixture(autouse=True)
def sqlite_db():
    """Fresh in-memory DB per test, bound into app.db. StaticPool = one shared conn."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    db.engine = engine
    db.SessionLocal.configure(bind=engine)
    yield engine
    Base.metadata.drop_all(engine)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)
