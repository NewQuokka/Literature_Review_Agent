"""#3 인용 대조: 수치·부호가 다른 인용은 확인됨이 되지 않고, 정확한 인용은 생략부호가 있어도 확인된다."""
import pytest

from conftest import add_paper, set_fulltext, write_pages
from lra import fulltext as FT

SOURCE = "Results show the program increased R&D spending by 1.5 percent (p > 0.05); the coefficient was −0.12 overall."


def ev(pid, quote, page=None):
    return {"paper_id": pid, "verbatim_short_quote": quote, "pdf_page": page}


@pytest.fixture
def paper(project):
    paths, con = project
    pid = add_paper(con, "Do R&D subsidies crowd out private investment")
    write_pages(paths, pid, ["Title page\nIntroduction text.", SOURCE, "Discussion of other findings here."])
    set_fulltext(con, pid)
    return paths, con, pid


# 이슈 #3 표의 1~3행: 정규화로 기호가 사라져 확인됨이 되던 오판
@pytest.mark.parametrize("quote", [
    "increased R&D spending by 15 percent",          # 1.5 → 15
    "the coefficient was 0.12 overall",              # −0.12 → 0.12
    "R&D spending by 1.5 percent (p < 0.05)",        # > → <
])
def test_wrong_numbers_are_not_verified(paper, quote):
    paths, con, pid = paper
    status, note = FT.verify_evidence_item(con, paths, ev(pid, quote, 2))
    assert status != "VERIFIED_FULLTEXT", note


def test_exact_quote_verified(paper):
    paths, con, pid = paper
    assert FT.verify_evidence_item(con, paths, ev(pid, "increased R&D spending by 1.5 percent (p > 0.05)", 2))[0] \
        == "VERIFIED_FULLTEXT"


def test_typographic_differences_still_match(paper):
    """따옴표·하이픈 종류·공백 차이는 같은 인용으로 본다."""
    paths, con, pid = paper
    status, _ = FT.verify_evidence_item(con, paths, ev(pid, "the coefficient was -0.12  overall", 2))
    assert status == "VERIFIED_FULLTEXT"


@pytest.mark.parametrize("quote", [
    "the program increased R&D spending … the coefficient was −0.12",
    "the program increased R&D spending [...] the coefficient was −0.12",
    "the program increased R&D spending ... the coefficient was −0.12",
])
def test_ellipsis_quote_verified(paper, quote):
    paths, con, pid = paper
    assert FT.verify_evidence_item(con, paths, ev(pid, quote, 2))[0] == "VERIFIED_FULLTEXT"


def test_ellipsis_segments_must_be_in_order(paper):
    paths, con, pid = paper
    quote = "the coefficient was −0.12 … the program increased R&D spending"
    assert FT.verify_evidence_item(con, paths, ev(pid, quote, 2))[0] != "VERIFIED_FULLTEXT"


def test_short_quote_not_verified(paper):
    paths, con, pid = paper
    status, note = FT.verify_evidence_item(con, paths, ev(pid, "(p > 0.05)", 2))
    assert status == "UNVERIFIED" and "짧음" in note


def test_wrong_page_is_conflict(paper):
    paths, con, pid = paper
    assert FT.verify_evidence_item(con, paths, ev(pid, "increased R&D spending by 1.5 percent", 3))[0] == "CONFLICT"


def test_page_boundary_counts_start_page(project):
    paths, con = project
    pid = add_paper(con, "Boundary paper title words")
    write_pages(paths, pid, ["intro text. The estimated effect was", "large and positive across firms. more text"])
    set_fulltext(con, pid)
    assert FT.locate_quote(paths, pid, "The estimated effect was large and positive") == [1]


def test_quote_on_several_pages_needs_page(project):
    paths, con = project
    pid = add_paper(con, "Repeated sentence paper title")
    s = "public subsidies stimulate private research investment"
    write_pages(paths, pid, [s, "other", s])
    set_fulltext(con, pid)
    status, note = FT.verify_evidence_item(con, paths, ev(pid, s))
    assert status == "UNVERIFIED" and "여러 쪽" in note
    assert FT.verify_evidence_item(con, paths, ev(pid, s, 3))[0] == "VERIFIED_FULLTEXT"


def test_greek_and_cjk_are_kept():
    assert "β" in FT._squash(FT.norm_text("β = 0.3"))
    assert FT._squash(FT.norm_text("研究開発補助金")) == "研究開発補助金"


# 이슈 #3 표의 4행: 정규화 결과가 비던 인용이 초록 기반으로 인정되던 문제
def test_cjk_only_short_quote_not_abstract_based(project):
    paths, con = project
    pid = add_paper(con, "Abstract only paper title", abstract="本研究は研究開発補助金の効果を分析する。")
    status, _ = FT.verify_evidence_item(con, paths, ev(pid, "研究開発補助金"))
    assert status != "ABSTRACT_BASED"


def test_abstract_quote_matches(project):
    paths, con = project
    abstract = "We find that public R&D subsidies increase private investment by 12% in small firms."
    pid = add_paper(con, "Abstract only paper title", abstract=abstract)
    assert FT.verify_evidence_item(con, paths, ev(pid, "public R&D subsidies increase private investment by 12%"))[0] \
        == "ABSTRACT_BASED"
    assert FT.verify_evidence_item(con, paths, ev(pid, "public R&D subsidies increase private investment by 2%"))[0] \
        == "UNVERIFIED"
