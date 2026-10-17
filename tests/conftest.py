"""테스트 공통 설정: 도구 패키지(lra) 경로와 임시 프로젝트 픽스처. 네트워크는 쓰지 않는다."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/adaptive-literature-review/scripts"
sys.path.insert(0, str(SCRIPTS))

from lra import db, papers as P  # noqa: E402
from lra.config import Paths  # noqa: E402


@pytest.fixture
def project(tmp_path):
    paths = Paths(tmp_path)
    paths.ensure()
    con = db.connect(paths.db)
    con.execute("INSERT INTO projects(project_id, title, owner, created_at, search_mode) VALUES "
                "('PRJ-001', 'test', '', ?, 'INTERNATIONAL')", (db.now(),))
    con.commit()
    yield paths, con
    con.close()


def add_paper(con, title, doi=None, tier="CORE", abstract=None, year=2020):
    """최소 서지로 논문을 등록하고 tier에 올린다. P-ID를 돌려준다."""
    pid, _ = P.upsert(con, {"title": title, "doi": doi, "year": year, "authors": [{"name": "Kim Test"}],
                            "abstract": abstract}, None)
    if tier != "CANDIDATE":
        db.set_membership(con, pid, tier, "APPROVED", "test")
    con.commit()
    return pid


def write_pages(paths, pid, pages):
    """state/text/P-xxx/p001.txt … 쪽별 텍스트를 만든다."""
    d = paths.text / pid
    d.mkdir(parents=True, exist_ok=True)
    for i, t in enumerate(pages, 1):
        (d / f"p{i:03d}.txt").write_text(t, encoding="utf-8")


def set_fulltext(con, pid, status="FULL_PDF"):
    con.execute("INSERT OR REPLACE INTO fulltexts(paper_id, access_status) VALUES (?,?)", (pid, status))
    con.commit()
