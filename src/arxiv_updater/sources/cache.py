import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..config import get_settings


class DailyResponseCache:
    """Small filesystem cache for public source responses; never stores credentials."""

    def __init__(self, namespace: str, directory: Path | None = None) -> None:
        self.namespace = namespace
        self.directory = directory or Path(get_settings().source_cache_dir)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str, now: datetime | None = None) -> Path:
        day = (now or datetime.now(UTC)).date().isoformat()
        digest = hashlib.sha256(key.encode()).hexdigest()[:20]
        return self.directory / f"{self.namespace}-{day}-{digest}.cache"

    def get(self, key: str, *, max_age: timedelta | None = None) -> str | None:
        path = self._path(key)
        if not path.exists():
            return None
        if max_age is not None:
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if datetime.now(UTC) - modified_at > max_age:
                return None
        return path.read_text(encoding="utf-8")

    def put(self, key: str, content: str) -> None:
        self._path(key).write_text(content, encoding="utf-8")
