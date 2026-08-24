from __future__ import annotations

import re
import tomllib
from pathlib import Path

import arxiv_updater

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_release_version_metadata_stays_synchronized() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        project_version = tomllib.load(stream)["project"]["version"]

    citation = (PROJECT_ROOT / "CITATION.cff").read_text(encoding="utf-8")
    citation_match = re.search(r"^version:\s*([^\s]+)\s*$", citation, re.MULTILINE)

    assert citation_match is not None
    assert arxiv_updater.__version__ == project_version
    assert citation_match.group(1) == project_version
    assert (PROJECT_ROOT / "docs" / "releases" / f"v{project_version}.md").is_file()
