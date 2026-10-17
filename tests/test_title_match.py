"""#5 원문 매칭: 짧은 한국어 제목이 자동 1.0이 되지 않고, 다른 논문의 PDF가 엉뚱한 P-ID에 붙지 않는다."""
import pytest

from conftest import add_paper
from lra import fulltext as FT

pymupdf = FT.pymupdf


def make_pdf(path, pages):
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontname="korea", fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def test_short_korean_title_not_auto_one():
    assert FT.title_match_score("완전히 다른 논문 본문입니다", "R&D 정책의 효과") == 0.0
    assert FT.title_match_score("R&D 정책의 효과에 관한 실증 분석", "R&D 정책의 효과") == 1.0


def test_too_short_title_is_undecidable():
    assert FT.title_match_score("아무 본문", "효과") is None
    assert FT.title_match_score("any text", "Innovation") is None


def test_word_boundary_not_substring():
    # 'firm'이 'confirm'에, 'policy'가 'policymaker'에 걸리지 않는다
    assert FT.title_match_score("we confirm results for policymakers", "Firm growth and policy") == 0.0


def test_line_break_hyphen_restored():
    assert FT.title_match_score("Public subsi-\ndies and private investment", "Public subsidies and private investment") == 1.0


@pytest.mark.parametrize("name,expected", [
    ("p-007.pdf", "P-007"), ("P-7.pdf", "P-007"), ("P007_kim.pdf", "P-007"), ("논문_P-012.pdf", "P-012"),
    ("paper2020.pdf", None), ("scop7.pdf", None),
])
def test_pid_from_filename(name, expected):
    assert FT.pid_from_filename(name) == expected


def test_identity_check_rejects_other_doi_on_first_page():
    score, why = FT.identity_check("doi:10.1000/other\nSome Title Words Here", "Some Title Words Here unrelated",
                                   "Some Title Words Here Plus More Different Content", "10.1000/mine")
    assert why


def test_identity_check_short_title_needs_own_doi():
    assert FT.identity_check("본문", "본문", "효과", "10.1000/mine")[1]
    assert FT.identity_check("https://doi.org/10.1000/mine\n효과", "효과", "효과", "10.1000/mine")[1] is None


# 이슈 #5 완료 기준 1: 한국어 짧은 제목 논문에 무관한 PDF를 넣으면 거부
def test_unrelated_pdf_not_linked_to_short_korean_title(project, tmp_path):
    paths, con = project
    add_paper(con, "R&D 정책의 효과")
    pdf = make_pdf(tmp_path / "논문12.pdf", ["기후 변화와 농업 생산성에 관한 연구\n서론 본문"])
    with pytest.raises(SystemExit, match="매칭되지 않았습니다"):
        FT.ingest_user_file(con, paths, pdf, None)


def test_related_pdf_links_by_korean_title(project, tmp_path):
    paths, con = project
    pid = add_paper(con, "공공 연구개발 보조금이 민간 투자에 미치는 효과")
    pdf = make_pdf(tmp_path / "scan.pdf", ["공공 연구개발 보조금이 민간 투자에 미치는 효과\n김연구"])
    assert FT.ingest_user_file(con, paths, pdf, None)["paper_id"] == pid


# 이슈 #5 완료 기준 2: 다른 Core 논문의 DOI를 본문에 인용한 PDF가 엉뚱한 P-ID에 붙지 않음
def test_cited_doi_does_not_hijack(project, tmp_path):
    paths, con = project
    own = add_paper(con, "Subsidies and innovation outcomes in manufacturing", doi="10.1000/own")
    add_paper(con, "Another core paper on tax credits", doi="10.1000/cited")
    pdf = make_pdf(tmp_path / "x.pdf", ["Subsidies and innovation outcomes in manufacturing\nhttps://doi.org/10.1000/own",
                                         "As shown by prior work (https://doi.org/10.1000/cited) ..."])
    assert FT.ingest_user_file(con, paths, pdf, None)["paper_id"] == own


def test_two_known_dois_on_first_page_refused(project, tmp_path):
    paths, con = project
    add_paper(con, "First paper title words", doi="10.1000/a")
    add_paper(con, "Second paper title words", doi="10.1000/b")
    pdf = make_pdf(tmp_path / "x.pdf", ["Issue contents\n10.1000/a\n10.1000/b"])
    with pytest.raises(SystemExit, match="여러 논문의 DOI"):
        FT.ingest_user_file(con, paths, pdf, None)


# 이슈 #5 완료 기준 3: p-007.pdf가 P-007로 인식
def test_lowercase_filename_recognized(project, tmp_path):
    paths, con = project
    for i in range(7):
        add_paper(con, f"Placeholder paper number {i} title")
    pdf = make_pdf(tmp_path / "p-007.pdf", ["Placeholder paper number 6 title"])
    assert FT.ingest_user_file(con, paths, pdf, None)["paper_id"] == "P-007"
