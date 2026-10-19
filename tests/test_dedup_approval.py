"""#8 중복 판정, #7 승인·규칙 강제."""
import importlib.util
import io
import json
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest

from conftest import SCRIPTS, add_paper
from lra import db, papers as P, verify as V

spec = importlib.util.spec_from_file_location("lra_cli", SCRIPTS / "lra.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def run(fn, paths, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(SimpleNamespace(root=str(paths.root), no_cache=True, **kw))
    return buf.getvalue()


def rec(title, year=2020, author="Kim Minsu", origin=None, doi=None, title_en=None):
    r = {"title": title, "year": year, "authors": [{"name": author}], "doi": doi, "title_en": title_en}
    if origin:
        r["db_origin"] = origin
    return r


def pair_status(con, a, b):
    x, y = sorted((a, b))
    r = con.execute("SELECT status FROM duplicate_pairs WHERE a=? AND b=?", (x, y)).fetchone()
    return r[0] if r else None


# ---------------- #8 ----------------
def test_refetch_keeps_review_duplicate(project):
    """이슈 #8 재현: KCI 국문판과 OpenAlex 영문판이 검토 대기로 묶인 뒤 영문판이 다시 수집돼도 표시가 남는다."""
    _, con = project
    ko, _ = P.upsert(con, {**rec("연구개발 보조금의 효과", origin="KCI", title_en="Effects of R&D subsidies"),
                            "kci_id": "ART001"}, None)
    en_rec = {**rec("Effects of R&D subsidies", author="Kim M."), "openalex_id": "W123"}
    en, _ = P.upsert(con, en_rec, None)
    assert pair_status(con, ko, en) == "REVIEW_DUPLICATE"
    P.upsert(con, en_rec, None)   # 다음 Stage C 검색에서 재수집
    assert pair_status(con, ko, en) == "REVIEW_DUPLICATE"
    assert db.pending_duplicates(con, en) == [ko]


def test_refetch_keeps_human_distinct(project):
    _, con = project
    a = add_paper(con, "Shared title of two studies", doi="10.1000/a")
    b = add_paper(con, "Shared title of two studies", doi="10.1000/b")
    P.resolve_duplicate(con, a, b, "DISTINCT", "다른 연구")
    P.upsert(con, rec("Shared title of two studies", doi="10.1000/b"), None)
    assert pair_status(con, a, b) == "DISTINCT"


def test_several_candidates_all_kept(project):
    _, con = project
    x, _ = P.upsert(con, {**rec("혁신 정책 평가", origin="KCI", title_en="Innovation policy evaluation"), "kci_id": "K1"}, None)
    y, _ = P.upsert(con, {**rec("혁신 정책 평가", author="Lee", origin="KCI"), "kci_id": "K2"}, None)
    z, _ = P.upsert(con, {**rec("Innovation policy evaluation", author="Park"), "openalex_id": "W9"}, None)
    assert set(db.pending_duplicates(con, x)) == {y, z}


def test_translated_title_alone_does_not_merge(project):
    """DOI 없는 레코드가 번역 제목만 같고 저자 비교가 불가하면 BOTH로 병합하지 않는다."""
    _, con = project
    ko, _ = P.upsert(con, {**rec("연구개발 보조금의 효과", author="김민수", origin="KCI", title_en="Effects of R&D subsidies"),
                            "kci_id": "ART9"}, None)
    en, new = P.upsert(con, rec("Effects of R&D subsidies", author="Kim"), None)
    assert new and en != ko
    assert con.execute("SELECT db_origin FROM papers WHERE paper_id=?", (ko,)).fetchone()[0] == "KCI"
    assert pair_status(con, ko, en) == "REVIEW_DUPLICATE"


def test_same_title_same_author_still_merges(project):
    _, con = project
    a, _ = P.upsert(con, rec("A study of firm innovation"), None)
    b, new = P.upsert(con, rec("A study of firm innovation", author="M. Kim Minsu"), None)
    assert b == a and not new


def test_v04_catches_unlinked_versions_in_core(project):
    paths, con = project
    pre = add_paper(con, "Public subsidies and private research investment", doi="10.48550/arxiv.1")
    pub = add_paper(con, "Public subsidies and private research investment", doi="10.1000/pub")
    v04 = next(ln for ln in V.run(con, paths)[0].splitlines() if ln.startswith("| V04"))
    assert "**FAIL**" in v04 and "연결되지 않은 판본 후보" in v04
    P.resolve_duplicate(con, pre, pub, "DISTINCT", "다른 연구")
    v04 = next(ln for ln in V.run(con, paths)[0].splitlines() if ln.startswith("| V04"))
    assert "**FAIL**" not in v04


def test_link_rejects_self_and_chain_and_unlink_restores(project):
    paths, con = project
    a = add_paper(con, "Paper alpha title words", doi="10.1000/a")
    b = add_paper(con, "Paper beta title words", doi="10.1000/b")
    c = add_paper(con, "Paper gamma title words", doi="10.1000/c")
    kw = dict(review=False, distinct=None, note=None, unlink=None)
    with pytest.raises(SystemExit, match="같은 P-ID"):
        run(cli.cmd_dedup, paths, link=[a, a], **kw)
    run(cli.cmd_dedup, paths, link=[a, b], **kw)
    assert db.membership(con, b)["status"] == "REJECTED"
    with pytest.raises(SystemExit, match="이미"):
        run(cli.cmd_dedup, paths, link=[b, c], **kw)   # 판본을 대표판으로 쓰는 연쇄 연결
    with pytest.raises(SystemExit, match="대표판으로 둔 판본"):
        run(cli.cmd_dedup, paths, link=[c, a], **kw)    # a를 c의 판본으로 → b가 고아가 됨
    run(cli.cmd_dedup, paths, link=None, **{**kw, "unlink": b})
    assert con.execute("SELECT canonical_work_id FROM papers WHERE paper_id=?", (b,)).fetchone()[0] is None
    assert db.membership(con, b)["status"] == "PROPOSED"
    assert pair_status(con, a, b) == "REVIEW_DUPLICATE"


def test_legacy_review_rows_migrated(project):
    paths, con = project
    a = add_paper(con, "Legacy one", doi="10.1000/l1")
    b = add_paper(con, "Legacy two", doi="10.1000/l2")
    con.execute("DELETE FROM duplicate_pairs")
    con.execute("UPDATE paper_external_ids SET match_status='REVIEW_DUPLICATE', related_paper_id=? WHERE paper_id=?", (a, b))
    con.commit()
    con2 = db.connect(paths.db)
    assert pair_status(con2, a, b) == "REVIEW_DUPLICATE"
    con2.close()


# ---------------- #7 ----------------
def test_approve_requires_by_and_user_evidence(project):
    paths, con = project
    pid = add_paper(con, "Seed paper title words", tier="CORE")
    with pytest.raises(SystemExit, match="--evidence"):
        run(cli.cmd_approve, paths, papers=[pid], by="user", evidence=None, note=None)
    run(cli.cmd_approve, paths, papers=[pid], by="user", evidence="네, 세 편 승인합니다", note=None)
    m = db.membership(con, pid)
    assert m["status"] == "APPROVED" and m["review_by"] == "user" and "세 편 승인" in m["reason"]


def test_approve_by_is_required_in_parser():
    import subprocess
    import sys
    r = subprocess.run([sys.executable, str(SCRIPTS / "lra.py"), "approve", "P-001"], capture_output=True, text=True)
    assert r.returncode != 0 and "--by" in r.stderr


def test_approve_rejects_candidate_and_rejected(project):
    paths, con = project
    cand = add_paper(con, "Candidate only paper", tier="CANDIDATE")
    with pytest.raises(SystemExit, match="승인 대상이 아닙니다"):
        run(cli.cmd_approve, paths, papers=[cand], by="agent", evidence=None, note=None)


def test_agent_approve_keeps_provisional_seed(project):
    paths, con = project
    pid = add_paper(con, "Provisional seed paper", tier="CANDIDATE")
    run(cli.cmd_seed, paths, action="set", role="theory", paper=pid, reason="r", alternatives=None, provisional=True,
        user_consented_mix=None)
    run(cli.cmd_approve, paths, papers=[pid], by="agent", evidence=None, note=None)
    assert con.execute("SELECT provisional FROM seeds WHERE paper_id=?", (pid,)).fetchone()[0] == 1


def test_kci_mode_seed_needs_consent_and_v17(project):
    paths, con = project
    con.execute("UPDATE projects SET search_mode='KCI'")
    con.commit()
    pid = add_paper(con, "Foreign database paper", tier="CANDIDATE")
    with pytest.raises(SystemExit, match="사용자 동의"):
        run(cli.cmd_seed, paths, action="set", role="theory", paper=pid, reason="r", alternatives=None,
            provisional=False, user_consented_mix=None)
    # 우회 경로(직접 Core 지정)도 V17이 FAIL로 잡는다
    db.set_membership(con, pid, "CORE", "PROPOSED", "x")
    con.commit()
    v17 = next(ln for ln in V.run(con, paths)[0].splitlines() if ln.startswith("| V17"))
    assert "**FAIL**" in v17
    run(cli.cmd_seed, paths, action="set", role="theory", paper=pid, reason="r", alternatives=None,
        provisional=False, user_consented_mix="사용자: 이론 원전은 해외 논문도 좋음")
    db.set_membership(con, pid, "CORE", "PROPOSED", "x", consent_note="사용자: 이론 원전은 해외 논문도 좋음")
    con.commit()
    v17 = next(ln for ln in V.run(con, paths)[0].splitlines() if ln.startswith("| V17"))
    assert "**FAIL**" not in v17


@pytest.mark.parametrize("flt,blocked", [
    ("primary_location.source.id:S1", True),
    ("primary_location.source.id:!S1,primary_location.source.id:S2", True),   # 부정 하나를 섞어도 걸림
    ("primary_location.source.issn:1234-5678", True),
    ("primary_location.source.host_organization:P1", True),
    ("primary_location.source.id:!S1|!S2", False),                            # 제외만
    ("primary_location.source.type:journal,publication_year:>2010", False),
    ("publication_year:2010-2020", False),
])
def test_source_restricting_clauses(flt, blocked):
    assert bool(cli.source_restricting_clauses(flt)) is blocked


def test_stage_c_filter_rejected(project):
    paths, con = project
    with pytest.raises(SystemExit, match="Stage C"):
        run(cli.cmd_search, paths, db=None, sources=None, stage="C", filter="primary_location.source.id:S1", query="x",
            purpose=None, field="title_and_abstract", limit=1, sort=None, feedback=None, expansion_reason=None, all_types=False)


@pytest.mark.parametrize("reason", ["주요 학술지 미수록", "non-preferred venue", "우선 출처 밖"])
def test_source_based_exclusion_rejected_everywhere(project, reason):
    paths, con = project
    pid = add_paper(con, "Some paper to exclude")
    with pytest.raises(SystemExit, match="제외 사유"):
        run(cli.cmd_deselect, paths, papers=[pid], reason=reason, status="REJECTED", category="quality", by="agent")
    with pytest.raises(SystemExit, match="제외 사유"):
        run(cli.cmd_screen, paths, papers=[pid], decision="exclude", reason=reason, stage="t", category="relevance")


def test_exclusion_requires_category(project):
    paths, con = project
    pid = add_paper(con, "Another paper to exclude")
    with pytest.raises(SystemExit, match="--category"):
        run(cli.cmd_deselect, paths, papers=[pid], reason="연구질문과 무관", status="REJECTED", category=None, by="agent")
    run(cli.cmd_deselect, paths, papers=[pid], reason="연구질문과 무관", status="REJECTED", category="relevance", by="agent")
    assert db.membership(con, pid)["reason"].startswith("[관련성]")


def test_user_excluded_source_not_revived(project):
    paths, con = project
    con.execute("""INSERT INTO preferred_sources(source_id, source_name, status, gap_id, created_at)
                   VALUES ('S123', 'X', 'USER_EXCLUDED', '', ?)""", (db.now(),))
    con.commit()
    with pytest.raises(SystemExit, match="사용자가 제외한 출처"):
        run(cli.cmd_source, paths, action="add", ids=["S123"], field=None, role=None, scope_fit=None, priority=None,
            reason=None, origin="agent", gap=None, user_restored=None, query=None, name=None, verify_title=None,
            kci_index_status=None, all_types=False)


def test_rank_approve_records_approver(project):
    from lra import ranking as RK
    _, con = project
    pid = add_paper(con, "Ranked paper words")
    con.execute("""INSERT INTO paper_rankings(paper_id, set_scope, version_id, importance_score, proposed_or_approved, is_current)
                   VALUES (?, 'CORE', ?, 50, 'PROPOSED', 1)""", (pid, db.current_version(con)))
    RK.approve(con, [pid], "user")
    assert con.execute("SELECT approved_by FROM paper_rankings WHERE paper_id=?", (pid,)).fetchone()[0] == "user"


def test_permissions_ask_file():
    cfg = json.loads((SCRIPTS.parents[3] / ".claude/settings.json").read_text(encoding="utf-8"))
    ask = cfg["permissions"]["ask"]
    assert any(r.endswith("lra.py approve:*)") for r in ask) and any("rank pin" in r for r in ask)
