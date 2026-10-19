"""#4 검증 체인, #11 검증 리포트: 미검증 근거의 본문 사용, 대조 텍스트 변조, N/A·INSUFFICIENT, 수치 대조, 종료 코드."""
import importlib.util
import io
import json
import os
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest

from conftest import SCRIPTS, add_paper
from lra import db, fulltext as FT, render as R, verify as V

spec = importlib.util.spec_from_file_location("lra_cli", SCRIPTS / "lra.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

PAGE2 = ("Results show the program increased R&D spending by 1.5 percent (p > 0.05); "
         "the coefficient was -0.12 overall. The sample includes 2412 firms.")


def args(paths, **kw):
    return SimpleNamespace(root=str(paths.root), no_cache=True, **kw)


def run_cli(fn, a):
    buf = io.StringIO()
    code = 0
    with redirect_stdout(buf):
        try:
            fn(a)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return code, buf.getvalue()


def line(md, vid):
    return next(ln for ln in md.splitlines() if ln.startswith(f"| {vid} "))


@pytest.fixture
def with_pdf(project, tmp_path):
    """원문 PDF가 연결된 Core 논문 하나."""
    paths, con = project
    pid = add_paper(con, "Do R&D subsidies crowd out private investment", doi="10.1000/rd")
    doc = FT.pymupdf.open()
    for text in ("Do R&D subsidies crowd out private investment", PAGE2):
        doc.new_page().insert_textbox(FT.pymupdf.Rect(50, 50, 550, 800), text, fontsize=10)
    doc.embfile_add("pad.bin", os.urandom(FT.MIN_PDF_BYTES))   # 유효 PDF 최소 크기 충족용(압축되지 않는 첨부)
    src = tmp_path / "src.pdf"
    doc.save(str(src))
    doc.close()
    FT.ingest_user_file(con, paths, src, pid)
    con.commit()
    return paths, con, pid


def add_evidence(paths, items):
    f = paths.root / "ev.json"
    f.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return run_cli(cli.cmd_evidence, args(paths, action="add", file=str(f), reextract=False))


# ---------------- #4 ----------------
def test_cite_replace_marks_unusable_evidence(with_pdf):
    paths, con, pid = with_pdf
    add_evidence(paths, [{"paper_id": pid, "verbatim_short_quote": "spending rose by 30 percent overall", "pdf_page": 2}])
    text = R.cite_replace(con, f"보조금은 효과가 있었다 [{pid}; E-001-01]", set())
    assert "⚠ UNVERIFIED" in text


def test_unverified_evidence_in_body_fails_v07(with_pdf):
    paths, con, pid = with_pdf
    add_evidence(paths, [{"paper_id": pid, "verbatim_short_quote": "spending rose by 30 percent overall", "pdf_page": 2}])
    (paths.state / "notes").mkdir(exist_ok=True)
    (paths.state / "notes" / "11_body.md").write_text(f"보조금은 민간 R&D를 30% 늘렸다 [{pid}; E-001-01]", encoding="utf-8")
    md, summ = V.run(con, paths)
    assert "**FAIL**" in line(md, "V07") and "E-001-01(UNVERIFIED)" in line(md, "V07")


def test_body_quote_must_match_evidence(with_pdf):
    paths, con, pid = with_pdf
    add_evidence(paths, [{"paper_id": pid, "verbatim_short_quote": "increased R&D spending by 1.5 percent", "pdf_page": 2}])
    (paths.state / "notes").mkdir(exist_ok=True)
    body = paths.state / "notes" / "09_body.md"
    body.write_text(f'저자들은 "increased R&D spending by 15 percent"라고 보고했다 [{pid}; E-001-01].', encoding="utf-8")
    assert "**FAIL**" in line(V.run(con, paths)[0], "V07")
    body.write_text(f'저자들은 "increased R&D spending by 1.5 percent"라고 보고했다 [{pid}; E-001-01].', encoding="utf-8")
    assert "**FAIL**" not in line(V.run(con, paths)[0], "V07")


def test_tampered_text_detected_and_reextract_fixes(with_pdf):
    paths, con, pid = with_pdf
    p2 = paths.text / pid / "p002.txt"
    p2.write_text(p2.read_text(encoding="utf-8").replace("1.5 percent", "15 percent"), encoding="utf-8")
    v06 = line(V.run(con, paths)[0], "V06")
    assert "**FAIL**" in v06 and "state/text" in v06
    run_cli(cli.cmd_evidence, args(paths, action="reverify", file=None, reextract=True))
    assert "state/text" not in line(V.run(con, paths)[0], "V06")


def test_auto_page_saved_and_printed_page_only_when_verified(with_pdf):
    paths, con, pid = with_pdf
    add_evidence(paths, [
        {"paper_id": pid, "verbatim_short_quote": "increased R&D spending by 1.5 percent", "printed_page": "312"},
        {"paper_id": pid, "verbatim_short_quote": "spending rose by 30 percent overall", "printed_page": "313"},
    ])
    rows = {r["evidence_id"]: r for r in con.execute("SELECT * FROM evidence")}
    assert rows["E-001-01"]["verification_status"] == "VERIFIED_FULLTEXT" and rows["E-001-01"]["pdf_page"] == 2
    assert rows["E-001-02"]["printed_page"] is None
    assert "인쇄 p.313" not in R.r08(con, paths)


# ---------------- #11 ----------------
def test_empty_project_is_not_all_pass(project):
    paths, con = project
    add_paper(con, "Some core paper title words")
    md, summ = V.run(con, paths)
    assert "**N/A**" in line(md, "V06") and "**N/A**" in line(md, "V07") and "**N/A**" in line(md, "V08")
    assert set(summ["na"]) >= {"V06", "V07", "V08"} and "N/A" in md.splitlines()[2]


def test_low_fulltext_rate_is_insufficient(with_pdf):
    paths, con, _ = with_pdf
    for i in range(3):
        add_paper(con, f"Extra paper without fulltext {i}")
        con.execute("INSERT OR REPLACE INTO fulltexts(paper_id, access_status) VALUES (?, 'REQUEST_USER')", (f"P-00{i + 2}",))
    con.commit()
    assert "**INSUFFICIENT**" in line(V.run(con, paths)[0], "V06")


@pytest.mark.parametrize("para,missing", [
    ("표본은 412개 기업이다", True),      # 원문 2412 안의 부분 문자열로 통과하던 오판
    ("표본은 2,412개 기업이다", False),   # 천단위 쉼표
    ("효과는 1.5%였다", False),           # % 표기 차이
    ("계수는 −0.12였다", False),          # 유니코드 마이너스
])
def test_v08_number_tokens(with_pdf, para, missing):
    paths, con, pid = with_pdf
    add_evidence(paths, [{"paper_id": pid, "type": "finding", "paraphrase": para, "pdf_page": 2}])
    v08 = line(V.run(con, paths)[0], "V08")
    assert ("**WARN**" in v08) is missing, v08


def test_v08_leading_zero():
    assert V.norm_num(".32") == "0.32" and V.norm_num("-.32") == "-0.32" and V.norm_num("−1,200%") == "-1200"


def test_bad_evidence_type_rejected_with_exit_code(with_pdf):
    paths, con, pid = with_pdf
    code, out = add_evidence(paths, [{"paper_id": pid, "type": "statistics", "paraphrase": "x"}])
    assert code == 1 and "rejected" in out


def test_verify_exit_code(with_pdf):
    paths, con, pid = with_pdf
    code, _ = run_cli(cli.cmd_verify, args(paths))
    assert code == 1   # 산출물 누락 등 FAIL이 있음
    md = (paths.out / "16_검증결과.md").read_text(encoding="utf-8")
    assert md.splitlines()[2].startswith("**FAIL")


def test_outside_reason_is_separate_field(project):
    paths, con = project
    pid = add_paper(con, "Paper outside preferred sources")
    db.set_membership(con, pid, "CORE", "PROPOSED", "핵심 실증", outside_reason=None)
    con.commit()
    assert "출처 밖 선정 사유(--outside-reason) 없음" in line(V.run(con, paths)[0], "V05")
    db.set_membership(con, pid, "CORE", "PROPOSED", "핵심 실증", outside_reason="분야 밖이지만 유일한 패널 자료")
    db.set_membership(con, pid, "CORE", "APPROVED", "승인")   # 다른 갱신이 별도 사유를 지우지 않음
    con.commit()
    assert "outside-reason" not in line(V.run(con, paths)[0], "V05")
