"""#1·#6·#10 회귀 테스트(네트워크 없음): 캐시 만료, 조회 실패 처리, 비밀값 가리기, KCI 연도 필터."""
import importlib.util
import json
import os
import time

import pytest

from conftest import SCRIPTS, add_paper
from lra import db, papers as P, sources as S, verify as V
from lra.config import Http, HttpError, redact

spec = importlib.util.spec_from_file_location("lra_cli", SCRIPTS / "lra.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class FakeResp:
    def __init__(self, body, status=200):
        self.status_code, self._body, self.headers, self.text = status, body, {}, json.dumps(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, body):
        self.body, self.calls, self.headers = body, 0, {}

    def get(self, *a, **k):
        self.calls += 1
        return FakeResp(self.body)


# ---- #1 캐시 만료 ----
def test_max_age_refetches_stale_cache(project):
    paths, _ = project
    http = Http(paths)
    http.s = FakeSession({"v": 1})
    url = "https://api.example.org/x"
    assert http.get_json(url, {}) == {"v": 1}
    http.s.body = {"v": 2}
    assert http.get_json(url, {}, max_age=3600) == {"v": 1} and http.s.calls == 1
    ck = http._key(url, {})
    old = time.time() - 2 * 86400
    os.utime(ck, (old, old))
    assert http.get_json(url, {}) == {"v": 1}                  # 만료 없음이면 그대로
    assert http.get_json(url, {}, max_age=86400) == {"v": 2}   # 하루 지난 캐시는 다시 받음


# ---- #10 비밀값 가리기 ----
def test_redact():
    assert redact("u?api_key=AB&mailto=a%40b.c&key=Z1 x") == "u?api_key=***&mailto=***&key=*** x"


def test_connection_error_is_redacted(project):
    paths, _ = project
    http = Http(paths, use_cache=False)
    with pytest.raises(HttpError) as e:
        http.request("http://127.0.0.1:9/works", {"api_key": "SECRET123", "mailto": "me@example.com"}, retries=1)
    assert "SECRET123" not in str(e.value) and "me@example.com" not in str(e.value)
    assert e.value.__cause__ is None


def test_search_value_strips_commas():
    assert S.search_value("innovation, R&D  subsidies") == "innovation R&D subsidies"


@pytest.mark.parametrize("flt,expected", [
    ("publication_year:2010-2020", ("201001", "202012")), ("publication_year:>2009", ("201001", None)),
    ("publication_year:<2021", (None, "202012")), ("publication_year:2015", ("201501", "201512")),
    ("publication_year:2010-", ("201001", None)), ("publication_year:-2020", (None, "202012")), (None, (None, None)),
])
def test_kci_years(flt, expected):
    assert cli._kci_years(flt) == expected


@pytest.mark.parametrize("flt", ["type:article", "publication_year:>2009,type:article", "publication_year:abc"])
def test_kci_years_rejects_unsupported(flt):
    with pytest.raises(SystemExit):
        cli._kci_years(flt)


# ---- #6 조회 실패 처리 ----
class CR:
    def __init__(self, work):
        self._work = work

    def work(self, doi, max_age=None):
        if isinstance(self._work, Exception):
            raise self._work
        return self._work

    def notices_updating(self, doi, max_age=None):
        return []


def test_doi_path_encodes_fragment():
    assert S.doi_path("10.1002/(sici)1097;2-#") == "10.1002/%28sici%291097%3B2-%23"


def test_crossref_404_is_unknown(project):
    _, con = project
    pid = add_paper(con, "Preprint on arxiv", doi="10.48550/arxiv.1706.03762")
    r = P.integrity_check(con, CR(None), pid)
    assert r["status"] == "UNKNOWN" and any("404" in (a.get("detail") or "") for a in r["alerts"])


def test_failure_keeps_previous_retraction(project):
    _, con = project
    pid = add_paper(con, "Retracted paper title", doi="10.1/x")
    assert P.integrity_check(con, CR({"updated-by": [{"type": "retraction"}]}), pid)["status"] == "RETRACTED"
    assert P.integrity_check(con, CR(HttpError("down")), pid)["status"] == "RETRACTED"
    assert P.integrity_check(con, CR({}), pid)["status"] == "NO_KNOWN_ALERT"   # 해제는 성공한 조회로만
    assert P.integrity_check(con, CR(HttpError("down")), pid)["status"] == "UNKNOWN"


def test_v14_warns_on_unknown(project):
    paths, con = project
    pid = add_paper(con, "Unknown status paper", doi="10.1/y")
    P.integrity_check(con, CR(None), pid)
    con.commit()
    md, _ = V.run(con, paths)
    v14 = next(ln for ln in md.splitlines() if ln.startswith("| V14"))
    assert "**WARN**" in v14 and f"조회 실패(UNKNOWN) ['{pid}']" in v14


def test_version_note_not_duplicated(project):
    _, con = project
    pid = add_paper(con, "Has preprint relation", doi="10.1/z")
    rel = {"relation": {"has-preprint": [{"id": "10.31234/osf.io/abc"}]}}
    for _ in range(3):
        P.integrity_check(con, CR(rel), pid)
    note = con.execute("SELECT version_note FROM papers WHERE paper_id=?", (pid,)).fetchone()[0]
    assert note.count("has-preprint") == 1
