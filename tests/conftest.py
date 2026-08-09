import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEST_DB = Path(tempfile.gettempdir()) / "arxiv-updater-pytest.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"
os.environ["DEEPSEEK_API_KEY"] = ""
os.environ["SERPAPI_API_KEY"] = ""

from arxiv_updater import db as db_module  # noqa: E402
from arxiv_updater import models as models_module  # noqa: E402
from arxiv_updater import web as web_module  # noqa: E402


@pytest.fixture()
def app_client():
    db_module.Base.metadata.drop_all(bind=db_module.engine)
    with db_module.engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    db_module.Base.metadata.create_all(bind=db_module.engine)
    with TestClient(web_module.create_app()) as client:
        yield client, db_module.SessionLocal, models_module
