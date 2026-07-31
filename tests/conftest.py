import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEST_DB = Path(tempfile.gettempdir()) / "arxiv-updater-pytest.db"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"
os.environ["APP_SECRET_KEY"] = "test-secret"
os.environ["LOCAL_DEV_AUTO_LOGIN"] = "false"

from arxiv_updater import db as db_module  # noqa: E402
from arxiv_updater import models as models_module  # noqa: E402
from arxiv_updater import web as web_module  # noqa: E402


@pytest.fixture()
def app_client():
    db_module.Base.metadata.drop_all(bind=db_module.engine)
    db_module.Base.metadata.create_all(bind=db_module.engine)
    with TestClient(web_module.create_app()) as client:
        yield client, db_module.SessionLocal, models_module
