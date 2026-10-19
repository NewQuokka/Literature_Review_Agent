"""#12 출력 형식(BibTeX·APA·CSV·본문 인용), #9 중요도 순위."""
import csv
import json
import shutil
import subprocess

import pytest

from conftest import add_paper
from lra import db, ranking as RK, render as R, verify as V


def row(con, pid):
    return con.execute("SELECT * FROM papers WHERE paper_id=?", (pid,)).fetchone()


# ---------------- #12 ----------------
def test_bibtex_escapes_and_protects(project):
    paths, con = project
    pid = add_paper(con, "Do R&D subsidies crowd out 100% of private_investment? A $ test", doi="10.1000/rd")
    con.execute("UPDATE papers SET authors_json=?, venue=? WHERE paper_id=?",
                (json.dumps([{"name": "World Bank Group"}, {"name": "Minsu Kim"}]), "Research & Policy", pid))
    bib = R.bibtex(con, paths)
    assert r"R\&D" in bib and r"100\%" in bib and r"private\_investment" in bib and r"\$" in bib
    assert "title = {{Do R" in bib                       # 이중 중괄호로 대소문자 보호
    assert "author = {{World Bank Group} and Kim, Minsu}" in bib
    assert r"journal = {Research \& Policy}" in bib
    assert "note =" not in bib and "x-lra = {P-001" in bib


@pytest.mark.skipif(not (shutil.which("pdflatex") and shutil.which("bibtex")), reason="LaTeX 미설치")
def test_bibtex_compiles(project, tmp_path):
    paths, con = project
    add_paper(con, "Do R&D subsidies crowd out 100% of private_investment", doi="10.1000/rd")
    (tmp_path / "refs.bib").write_text(R.bibtex(con, paths), encoding="utf-8")
    (tmp_path / "t.tex").write_text("\\documentclass{article}\\begin{document}\\nocite{*}"
                                    "\\bibliographystyle{plain}\\bibliography{refs}\\end{document}\n", encoding="utf-8")
    for cmd in (["pdflatex", "-interaction=nonstopmode", "t"], ["bibtex", "t"], ["pdflatex", "-interaction=nonstopmode", "t"]):
        r = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:]


def test_apa_keeps_ellipsis_and_no_double_period(project):
    _, con = project
    pid = add_paper(con, "Innovation... and beyond", doi="10.1000/x")
    con.execute("UPDATE papers SET authors_json=? WHERE paper_id=?",
                (json.dumps([{"name": f"A{i} Author{i}"} for i in range(25)]), pid))
    s = R.apa(row(con, pid))
    assert "Innovation... and beyond." in s and ", ... " in s and ".." not in s.replace("...", "")
    pid2 = add_paper(con, "Why do firms innovate?", doi="10.1000/y")
    assert R.apa(row(con, pid2)).endswith("Why do firms innovate? https://doi.org/10.1000/y")


@pytest.mark.parametrize("text,expect", [
    ("[P-001, P-002]", ["Kim, 2020", "Lee, 2021", "[P-001 / P-002]"]),
    ("[P-001; P-002]", ["Kim, 2020; Lee, 2021"]),
    ("[P-001; E-001-01; P-002]", ["[P-001; E-001-01 / P-002]"]),
    ("[P-001, p. 12]", ["(Kim, 2020, p. 12 [P-001])"]),
    ("[P-001]", ["(Kim, 2020 [P-001])"]),
])
def test_cite_replace_multi(project, text, expect):
    _, con = project
    a = add_paper(con, "First paper", year=2020)
    b = add_paper(con, "Second paper", year=2021)
    con.execute("UPDATE papers SET authors_json=? WHERE paper_id=?", (json.dumps([{"name": "J Kim"}]), a))
    con.execute("UPDATE papers SET authors_json=? WHERE paper_id=?", (json.dumps([{"name": "S Lee"}]), b))
    con.execute("INSERT INTO evidence(evidence_id, paper_id, verification_status) VALUES ('E-001-01', ?, 'VERIFIED_FULLTEXT')", (a,))
    out = R.cite_replace(con, text, set())
    for e in expect:
        assert e in out, out


def test_eid_inside_multi_bracket_checked(project):
    _, con = project
    a = add_paper(con, "First paper")
    b = add_paper(con, "Second paper")
    con.execute("INSERT INTO evidence(evidence_id, paper_id, verification_status) VALUES ('E-001-01', ?, 'VERIFIED_FULLTEXT')", (a,))
    assert f"⚠ {b}의 근거 아님" in R.cite_replace(con, f"[{a}; {b}; E-001-01]", set())


def test_unparsed_citation_marked(project):
    _, con = project
    add_paper(con, "First paper")
    assert R.UNPARSED_CITE in R.cite_replace(con, "[P-001 see table 3]", set())


def test_citing_excluded_paper_fails_v15(project):
    paths, con = project
    a = add_paper(con, "Kept paper")
    b = add_paper(con, "Rejected version paper")
    db.set_membership(con, b, "CORE", "REJECTED", "판본 통합")
    (paths.state / "notes").mkdir(exist_ok=True)
    (paths.state / "notes" / "11_body.md").write_text(f"선행연구는 [{a}; {b}]에서 보듯", encoding="utf-8")
    con.commit()
    v15 = next(ln for ln in V.run(con, paths)[0].splitlines() if ln.startswith("| V15"))
    assert "**FAIL**" in v15 and f"11:{b}" in v15


def test_csv_formula_cells_neutralized(project):
    paths, con = project
    pid = add_paper(con, "=HYPERLINK(\"http://x\")")
    db.set_membership(con, pid, "CORE", "PROPOSED", "- 이론 원전")
    con.commit()
    R.r03(con, paths)
    with open(paths.out / "03_핵심문헌_20편.csv", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    cells = [c for r in rows[1:] for c in r]
    assert "'=HYPERLINK(\"http://x\")" in cells and "'- 이론 원전" in cells
    assert R.csv_safe("-0.3") == "-0.3" and R.csv_safe("+12%") == "+12%"


def test_md_cell_carriage_return():
    assert "\r" not in R.md_cell("a\r\nb\rc")


# ---------------- #9 ----------------
def scored(con, scores_by_pid, tier="CORE", roles=None):
    items = []
    for pid, s in scores_by_pid.items():
        if roles:
            con.execute("UPDATE papers SET role_tags=? WHERE paper_id=?", (roles, pid))
        crit = dict(zip(db.CRITERIA, s))
        items.append({"paper_id": pid, **crit, "rationale": {c: "근거" for c, v in crit.items() if v is not None}, "tier": "B"})
    return RK.add_scores(con, items)


def labels(con, scope="CORE"):
    return {r["paper_id"]: r["rank_label"] for r in RK.current_rows(con, scope)}


def test_ties_survive_pin(project):
    """이슈 #9 예시: 80/70/70/60, 60점을 1위로 고정 → 1 · 2 · 3= · 3=."""
    _, con = project
    p = [add_paper(con, f"Paper {i}") for i in range(4)]
    scored(con, {p[0]: (4, 4, 4, 4, 4), p[1]: (3.5,) * 5, p[2]: (3.5,) * 5, p[3]: (3, 3, 3, 3, 3)})
    RK.pin(con, p[3], 1, "사용자 지정", "user")
    lab = labels(con)
    assert lab[p[3]] == "1" and lab[p[0]] == "2" and lab[p[1]] == lab[p[2]] == "3="


def test_role_ranks_have_ties(project):
    _, con = project
    p = [add_paper(con, f"Role paper {i}") for i in range(3)]
    scored(con, {p[0]: (4,) * 5, p[1]: (3,) * 5, p[2]: (3,) * 5}, roles="theory")
    rr = {r["paper_id"]: r["theory_rank"] for r in RK.current_rows(con, "CORE")}
    assert rr[p[0]] == 1 and rr[p[1]] == rr[p[2]] == 2


def test_pin_conflict_and_bad_rank_rejected(project):
    _, con = project
    p = [add_paper(con, f"Pin paper {i}") for i in range(2)]
    scored(con, {p[0]: (4,) * 5, p[1]: (3,) * 5})
    RK.pin(con, p[0], 1, "r", "user")
    with pytest.raises(SystemExit, match="이미 1위로 고정"):
        RK.pin(con, p[1], 1, "r", "user")
    with pytest.raises(SystemExit, match="1 이상"):
        RK.pin(con, p[1], 0, "r", "user")


def test_negative_weight_rejected(project):
    _, con = project
    w = {c: 0 for c in db.CRITERIA}
    w.update({"research_fit": 2, "role_contribution": -1})
    with pytest.raises(SystemExit, match="0 이상 1 이하"):
        RK.set_weights(con, w, "x", "user")


def test_previous_version_not_mixed(project):
    _, con = project
    p = [add_paper(con, f"Version paper {i}") for i in range(2)]
    scored(con, {p[0]: (4,) * 5, p[1]: (3,) * 5})
    con.execute("UPDATE paper_rankings SET version_id='V-OLD' WHERE paper_id=?", (p[1],))
    RK.recompute(con, "CORE")
    assert set(labels(con)) == {p[0]}


def test_provisional_score_when_only_evidence_quality_missing(project):
    _, con = project
    p = [add_paper(con, f"Prov paper {i}") for i in range(2)]
    res = scored(con, {p[0]: (5, 4, None, 2, 4), p[1]: (5, None, None, 2, 4)})
    assert res[0]["score"] is not None and "provisional" in res[0]
    assert res[1]["score"] is None and res[1]["status"] == "PENDING_EVIDENCE"
    lab = labels(con)
    assert lab[p[0]].endswith("(잠정)") and lab[p[1]] == "평가보류"


def test_v16_flags_pin_conflict_inserted_directly(project):
    paths, con = project
    p = [add_paper(con, f"V16 paper {i}") for i in range(2)]
    scored(con, {p[0]: (4,) * 5, p[1]: (3,) * 5})
    con.execute("UPDATE paper_rankings SET pinned_rank=1, override_reason='x' WHERE is_current=1")
    con.commit()
    v16 = next(ln for ln in V.run(con, paths)[0].splitlines() if ln.startswith("| V16"))
    assert "**FAIL**" in v16 and "겹친 Pin" in v16
