"""최종 검증(V01·V04·V06·V07·V08·V13·V14·V15 + 원본 불변). 결과는 out/16_검증결과.md.
사람 평가가 필요한 지표(V02·V03·V05·V09·V10·V11·V12)는 '미검증'으로 남긴다 — 임의 합격 처리 금지."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import db
from .config import (CORE_TARGET, INTEGRITY_STALE_DAYS, MIN_FULLTEXT_RATE, MIN_VERIFIED_QUOTE_RATE,
                     QUOTE_MAX_WORDS, Paths)
from .fulltext import MIN_PDF_BYTES, TITLE_MATCH_MIN, sha256, text_drift, verify_evidence_item, page_text
from .papers import norm_title
from .render import member_rows

REQUIRED = ["01_연구맥락_검색전략.md", "01a_우선학술출처_선정표.md", "03a_논문별_중요도_순위표.md", "02_Seed_3편_선정표.md", "03_핵심문헌_20편.csv", "04_검색_선별_이력.md",
            "05_원문확보_현황.csv", "06_원문다운로드_요청목록.md", "07_논문별_분석카드.md", "08_인용근거_라이브러리.md",
            "09_선행연구_비교분석.md", "10_연구계획_문헌활용계획.md", "11_선행연구_종합보고서.md", "12_연구공백_및_확장계획.md"]
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s|)\]<>`\"',;]+")
NUM_RE = re.compile(r"(?<![\w.,])[-−–]?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?(?![\d.,]\d)"
                    r"|(?<![\w.,])[-−–]?(?:\d+(?:\.\d+)?|\.\d+)%?(?![\d.,]\d)")   # 천단위 쉼표 숫자·소수 앞 0 생략(.32)


def norm_num(n: str) -> str:
    """비교용 숫자 표기: 유니코드 마이너스·천단위 쉼표·% 제거, '.32'→'0.32'(#11)."""
    n = n.replace("−", "-").replace("–", "-").replace(",", "").rstrip("%")
    if n.startswith("-."):
        n = "-0" + n[1:]
    elif n.startswith("."):
        n = "0" + n
    return n


QUOTE_MARKS_RE = re.compile(r"[“\"]([^”\"\n]{8,400})[”\"]")
CITE_NEAR = 160   # 직접 인용 뒤 이 글자 수 안의 [P-xxx; E-xxx-yy]를 그 인용의 근거로 본다


def body_citation_issues(con, paths: Paths, status_now: dict) -> tuple[list, list, list]:
    """보고서 본문 초안(09·11)에서 (사용 금지 근거 인용, 근거와 다른 직접 인용, 근거 없는 직접 인용)(#4)."""
    from .fulltext import find_segments, norm_text, quote_segments, _squash
    bad, mismatch, orphan = [], [], []
    for key in ("09", "11"):
        f = paths.state / "notes" / f"{key}_body.md"
        if not f.exists():
            continue
        t = f.read_text(encoding="utf-8")
        for eid in sorted(set(re.findall(r"E-\d{3,}-\d{2,}", t))):
            st = status_now.get(eid)
            if st in ("UNVERIFIED", "CONFLICT", "METADATA_ONLY"):
                bad.append(f"{key}:{eid}({st})")
        for m in QUOTE_MARKS_RE.finditer(t):
            segs, why = quote_segments(m.group(1))
            if why:   # 강조용 짧은 따옴표는 직접 인용으로 보지 않음
                continue
            near = re.findall(r"E-\d{3,}-\d{2,}", t[m.end():m.end() + CITE_NEAR])
            if not near:
                orphan.append(f"{key}:\"{m.group(1)[:30]}…\"")
                continue
            ok = False
            for eid in near:
                row = con.execute("SELECT verbatim_short_quote FROM evidence WHERE evidence_id=?", (eid,)).fetchone()
                if row and row[0] and find_segments(_squash(norm_text(row[0])), segs):
                    ok = True
                    break
            if not ok:
                mismatch.append(f"{key}:\"{m.group(1)[:30]}…\"→{near}")
    return bad, mismatch, orphan


def raw_manifest(paths: Paths) -> dict:
    out = {}
    for f in sorted(paths.raw.rglob("*")):
        if f.is_file():
            out[str(f.relative_to(paths.root)).replace("\\", "/")] = sha256(f)
    return out


def run(con, paths: Paths) -> tuple[str, dict]:
    res = []   # (id, name, verdict, detail)

    def add(i, n, v, d):
        res.append((i, n, v, d))

    core = member_rows(con)
    ids = {r["paper_id"] for r in core}

    # V01 서지정보
    bad = [f"{r['paper_id']}({r['bib_match']})" for r in core if r["bib_match"] in ("MISMATCH", "DOI_NOT_FOUND")]
    unchecked = [r["paper_id"] for r in core if not r["bib_match"]]
    nodoi = [r["paper_id"] for r in core if not r["doi"]]
    add("V01", "서지정보 정확성(Crossref 대조)", "FAIL" if bad else ("WARN" if unchecked or nodoi else "PASS"),
        f"불일치 {len(bad)}건 {bad}; 미점검 {len(unchecked)}건 {unchecked[:10]}; DOI 없음 {len(nodoi)}건 {nodoi} (자동 제외하지 않음)")

    # V04 중복
    from .papers import distinct_pairs, find_version_duplicates
    distinct = distinct_pairs(con)   # 사람이 '별개 연구'로 판정한 쌍은 중복으로 세지 않음
    dups = []
    seen_doi, seen_t = {}, {}
    for r in core:
        if r["canonical_work_id"]:
            continue
        if r["doi"]:
            if r["doi"] in seen_doi:
                dups.append((seen_doi[r["doi"]], r["paper_id"]))
            seen_doi[r["doi"]] = r["paper_id"]
        t = norm_title(r["title"])
        if t in seen_t and tuple(sorted((seen_t[t], r["paper_id"]))) not in distinct:
            dups.append((seen_t[t], r["paper_id"]))
        seen_t[t] = r["paper_id"]
    linked_in = [r["paper_id"] for r in core if r["canonical_work_id"] and r["canonical_work_id"] in ids]
    # 유사도 기반 판본 후보·검토 대기 중복이 연결 없이 함께 집합에 있으면 한 연구가 두 자리를 차지한다(#8)
    versions = [(d["a"], d["b"]) for d in find_version_duplicates(con)
                if d["a"] in ids and d["b"] in ids and not d["already_linked"] and tuple(sorted((d["a"], d["b"]))) not in distinct]
    pending = [(r[0], r[1]) for r in con.execute("SELECT a, b FROM duplicate_pairs WHERE status='REVIEW_DUPLICATE'")
               if r[0] in ids and r[1] in ids]
    n_runs = con.execute("SELECT COUNT(*) FROM search_runs").fetchone()[0]
    add("V04", "중복·선별 이력", "FAIL" if dups or linked_in or versions or pending else ("PASS" if n_runs else "WARN"),
        f"중복 의심 {dups}; 대표판과 함께 집합에 남은 판본 {linked_in}; 연결되지 않은 판본 후보 {versions} "
        f"(`dedup --link` 또는 `--distinct`); 검토 대기 중복이 함께 선정됨 {pending}; 검색 Run {n_runs}건 기록")

    # 문헌 수
    n_core = sum(1 for r in core if r["tier"] == "CORE")
    n_ext = sum(1 for r in core if r["tier"] == "EXTENDED")
    n_appr = sum(1 for r in core if r["tier"] == "CORE" and r["status"] == "APPROVED")
    add("CNT", "Core 문헌 수(목표 20, 의무 아님)", "PASS" if n_core >= CORE_TARGET else "WARN",
        f"Core {n_core}편(승인 {n_appr}) / Extended {n_ext}편" + (f" — {CORE_TARGET - n_core}편 미충족, 사유를 보고서에 기재" if n_core < CORE_TARGET else ""))
    seeds = con.execute("SELECT role, status FROM seeds WHERE status!='REJECTED'").fetchall()
    miss = {"theory", "methodology", "empirical"} - {s["role"] for s in seeds}
    add("SEED", "Seed 역할별 1편", "WARN" if miss else "PASS",
        f"누락 역할 {sorted(miss)}; 승인 상태 " + ", ".join(f"{s['role']}={s['status']}" for s in seeds))

    # V05 검색 포괄성·우선 출처 편향(자동 가능한 부분만)
    runs = con.execute("SELECT * FROM search_runs").fetchall()
    stages = {db.run_stage(r) for r in runs}
    srcs = con.execute("SELECT * FROM preferred_sources WHERE status='ACTIVE'").fetchall()
    v5_fail, v5_warn = [], []
    if "A" not in stages:
        v5_warn.append("Stage A(우선 출처) 검색 기록 없음")
    if "B" not in stages:
        v5_fail.append("Stage B(제한 없는 인용망) 미실행")
    if "C" not in stages:
        v5_fail.append("Stage C(독립·광역 검증) 미실행 — 최소 1회 필수")
    seed_runs = [r["set_at"] for r in con.execute("SELECT set_at FROM seeds")]
    c_runs = [r["run_date"] for r in runs if db.run_stage(r) == "C"]
    if seed_runs and c_runs and max(c_runs) < min(seed_runs):
        v5_warn.append("Stage C가 Seed 선정 이전에만 실행됨 — Seed 이후 독립 검증 권장")
    for r in runs:
        if db.run_stage(r) in ("B", "C") and r["source_ids"]:
            v5_fail.append(f"{r['run_id']}: Stage {db.run_stage(r)}에 출처 제한 적용")
    unknown_src = [r["run_id"] for r in runs if r["source_ids"] and
                   any(s not in {x["source_id"] for x in con.execute("SELECT source_id FROM preferred_sources")} for s in r["source_ids"].split(","))]
    if unknown_src:
        v5_fail.append(f"등록되지 않은 출처 ID로 검색: {unknown_src}")
    if not srcs:
        v5_warn.append("우선 학술출처 미등록")
    elif not (5 <= len({s['source_id'] for s in srcs if not s['gap_id']}) <= 10):
        v5_warn.append(f"기본 우선 출처 {len({s['source_id'] for s in srcs if not s['gap_id']})}곳(권고 5~10, 조정 사유 기록 필요)")
    bad_reason = [d["paper_id"] for d in con.execute("SELECT paper_id, reason FROM screening_decisions WHERE decision='exclude'")
                  if re.search(r"출처\s*밖|우선\s*출처|목록에\s*없", d["reason"] or "")]
    if bad_reason:
        v5_fail.append(f"'우선 출처 밖'을 제외 사유로 사용: {bad_reason}")
    pref_ids = db.all_preferred_ids(con)
    outside_rows = [r for r in core if r["venue_source_id"] not in pref_ids]
    outside = [r["paper_id"] for r in outside_rows]
    no_reason = [r["paper_id"] for r in outside_rows if not (r["outside_reason"] or "").strip()]
    if no_reason:   # 일반 선정 사유(--reason, 필수)와 별도 필드로 확인한다(#11)
        v5_warn.append(f"우선 출처 밖 문헌의 출처 밖 선정 사유(--outside-reason) 없음: {no_reason}")
    no_src = [r["paper_id"] for r in core if not r["venue_source_id"] and r["openalex_id"]]
    if no_src:
        v5_warn.append(f"출처 ID 미기록(구버전 레코드 → `refresh` 실행): {no_src}")
    if not con.execute("SELECT 1 FROM search_coverage_checks").fetchone():
        v5_warn.append("coverage --record 미실행")
    add("V05", "검색 포괄성·우선 출처 편향(자동 부분)", "FAIL" if v5_fail else ("WARN" if v5_warn else "PASS"),
        f"Stage 실행 {sorted(stages)}; 우선 출처 밖 Core/Extended {len(outside)}편; 문제 {v5_fail + v5_warn}. "
        "중요 누락(Recall)은 전문가 정답 대조 필요")

    # V06 원문 파일
    issues, undecided, n_full = [], [], 0
    for ft in con.execute("SELECT * FROM fulltexts WHERE access_status IN ('FULL_PDF','FULL_XML')"):
        n_full += 1
        f = Path(ft["file_path"] or "")
        if not f.exists():
            issues.append(f"{ft['paper_id']}: 파일 없음")
            continue
        if sha256(f) != ft["checksum"]:
            issues.append(f"{ft['paper_id']}: 체크섬 불일치")
        if not f.read_bytes()[:4] == b"%PDF" or f.stat().st_size < MIN_PDF_BYTES:
            issues.append(f"{ft['paper_id']}: 유효 PDF 아님")
        if ft["title_match"] is None:
            undecided.append(ft["paper_id"])
        elif ft["title_match"] < TITLE_MATCH_MIN:
            issues.append(f"{ft['paper_id']}: 제목 일치도 {ft['title_match']}")
        if ft["paper_id"] not in f.name:
            issues.append(f"{ft['paper_id']}: 파일명-P-ID 불일치")
        drift = text_drift(con, paths, ft["paper_id"])
        if drift:
            issues.append(f"{ft['paper_id']}: 대조 기준 텍스트(state/text)가 PDF와 다름 p.{drift[:5]} "
                          "— `evidence reverify --reextract`")
    tried = con.execute("SELECT COUNT(*) FROM fulltexts").fetchone()[0]
    core_full = sum(1 for r in core if db.fulltext_status(con, r["paper_id"]) in ("FULL_PDF", "FULL_XML"))
    ft_rate = core_full / len(core) if core else 0.0
    v6 = ("FAIL" if issues else "N/A" if not tried else "INSUFFICIENT" if ft_rate < MIN_FULLTEXT_RATE
          else "WARN" if undecided else "PASS")
    add("V06", "원문 확보 정확성", v6,
        f"Core+Extended 원문 확보 {core_full}/{len(core)}편({ft_rate * 100:.0f}%, 기준 {MIN_FULLTEXT_RATE * 100:.0f}%)"
        + (f"; 유효 전문 {n_full}편 / 시도 {tried}편" if tried else "; 확보 시도 없음")
        + (f"; 문제 {issues}" if issues else "")
        + (f"; 제목 판정 불가(사람 확인) {undecided}" if undecided else ""))

    # V07 직접 인용 + 보고서 본문(09·11)에서의 사용(#4)
    v7, long_q, changed, n_quotes, n_ok = [], [], [], 0, 0
    status_now = {}
    for e in con.execute("SELECT * FROM evidence"):
        q = e["verbatim_short_quote"]
        if not q:
            status_now[e["evidence_id"]] = e["verification_status"]
            continue
        n_quotes += 1
        if len(q.split()) > QUOTE_MAX_WORDS:
            long_q.append(e["evidence_id"])
        st, note = verify_evidence_item(con, paths, dict(e))
        status_now[e["evidence_id"]] = st
        if st != e["verification_status"]:
            changed.append(f"{e['evidence_id']}: 기록 {e['verification_status']} → 재검증 {st}")
        if st in ("UNVERIFIED", "CONFLICT"):
            v7.append(f"{e['evidence_id']}({st})")
        elif st == "VERIFIED_FULLTEXT":
            n_ok += 1
    body_bad, body_quote_bad, body_quote_orphan = body_citation_issues(con, paths, status_now)
    q_rate = n_ok / n_quotes if n_quotes else 0.0
    if changed or long_q or body_bad or body_quote_bad:
        v7v = "FAIL"
    elif not n_quotes and not body_quote_orphan:
        v7v = "N/A"
    elif n_quotes and q_rate < MIN_VERIFIED_QUOTE_RATE:
        v7v = "INSUFFICIENT"
    else:
        v7v = "WARN" if v7 or body_quote_orphan else "PASS"
    add("V07", "직접인용 정확성(원문 대조)·본문 사용", v7v,
        (f"직접 인용 {n_quotes}건 중 원문 확인 {n_ok}건({q_rate * 100:.0f}%, 기준 {MIN_VERIFIED_QUOTE_RATE * 100:.0f}%)"
         if n_quotes else "등록된 직접 인용 없음")
        + f"; 미검증/충돌 {len(v7)}건 {v7[:15]}; 본문(09·11)에 인용한 사용 금지 근거 {body_bad}; "
        f"본문 직접 인용이 근거 발췌와 다름 {body_quote_bad}; 근거 ID 없는 본문 직접 인용 {body_quote_orphan}; "
        f"상태 변동 {changed}; {QUOTE_MAX_WORDS}단어 초과 {long_q}")

    # V08 수치 대조(재서술 속 숫자가 인용문·해당 쪽에 있는지)
    v8, n_num = [], 0
    for e in con.execute("SELECT * FROM evidence WHERE type IN ('finding','statistic','result')"):
        nums = {norm_num(n) for n in NUM_RE.findall(e["paraphrase"] or "")} - {"1", "2", "3"}
        if not nums:
            continue
        n_num += 1
        src = (e["verbatim_short_quote"] or "")
        if e["pdf_page"] and db.fulltext_status(con, e["paper_id"]) in ("FULL_PDF", "FULL_XML"):
            for pg in (int(e["pdf_page"]) - 1, int(e["pdf_page"]), int(e["pdf_page"]) + 1):   # 표가 다음 쪽으로 넘어가는 경우
                src += " " + (page_text(paths, e["paper_id"], pg) or "") if pg > 0 else ""
        src = re.sub(r"[−–]\s*(?=\d)", "-", src).replace(",", "")   # 유니코드 마이너스·천단위 쉼표 정규화
        if not src.strip():
            p = con.execute("SELECT abstract FROM papers WHERE paper_id=?", (e["paper_id"],)).fetchone()
            src = (p["abstract"] or "") if p else ""
        have = {norm_num(n) for n in NUM_RE.findall(src)}   # 부분 문자열이 아니라 숫자 토큰으로 대조(#11)
        missing = sorted(n for n in nums if n not in have)
        if missing:
            v8.append(f"{e['evidence_id']}: {missing}")
    add("V08", "연구결과 수치 대조", "N/A" if not n_num else ("WARN" if v8 else "PASS"),
        ("수치가 든 결과 근거 없음" if not n_num else
         f"수치 근거 {n_num}건; " + (f"원문/초록에서 찾지 못한 수치 {v8} — 사람이 확인" if v8 else "재서술의 수치가 모두 근거 텍스트에서 발견됨"))
        + " (부호·유의성 해석의 정확성은 사람 검토 필요)")

    # V13 라이선스·보안
    v13 = []
    for ft in con.execute("SELECT * FROM fulltexts WHERE access_status IN ('FULL_PDF','FULL_XML')"):
        if ft["source_type"] not in ("openalex_best_oa", "openalex_oa_location", "semanticscholar_oa", "user_provided"):
            v13.append(f"{ft['paper_id']}: 출처유형 {ft['source_type']}")
    pdf_out = [str(p) for p in paths.out.rglob("*.pdf")]
    if pdf_out:
        v13.append(f"out/에 PDF 존재(재배포 위험): {pdf_out}")
    add("V13", "라이선스·보안", "FAIL" if v13 else "PASS",
        "; ".join(v13) or "모든 원문이 OA 위치 또는 사용자 제공. out/에 원문 없음. 외부 전송은 검색식·DOI만(구조상).")

    # V14 출판상태 — 오래된 점검은 그 뒤 나온 철회 공지를 반영하지 못하므로 WARN
    # UNKNOWN(조회 실패·DOI 없음·Crossref 미등록)은 확인하지 못한 것이므로 PASS로 두지 않는다(#6)
    alerts, unchecked, unknown, follow, stale, checked = [], [], [], [], [], []
    limit = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - INTEGRITY_STALE_DAYS * 86400))
    for r in core:
        row = db.integrity_latest(con, r["paper_id"])
        if row is None:
            unchecked.append(r["paper_id"])
            continue
        s, at, fu = row["status"], row["checked_at"] or "", row["follow_up_required"]
        if s in ("RETRACTED", "EXPRESSION_OF_CONCERN", "CORRECTED"):
            alerts.append(f"{r['paper_id']}:{s}")
        elif s == "UNKNOWN":
            unknown.append(r["paper_id"])
        if fu:
            follow.append(r["paper_id"])
        checked.append(at)
        if at < limit:
            stale.append(r["paper_id"])
    retracted_in_core = [a for a in alerts if "RETRACTED" in a]
    last = f"마지막 점검 {min(checked)} ~ {max(checked)}" if checked else "점검 기록 없음"
    add("V14", "정정·철회 점검",
        "FAIL" if retracted_in_core else ("WARN" if unchecked or alerts or unknown or stale else "PASS"),
        f"경고 {alerts}; 조회 실패(UNKNOWN) {unknown}; 후속 확인 필요 {follow}; 미점검 {unchecked}; "
        f"{INTEGRITY_STALE_DAYS}일 초과 점검 {stale}; {last}. NO_KNOWN_ALERT는 철회 없음의 보증이 아님.")

    # V15 형식·완결성 + 허구 ID/DOI 탐지
    missing = [f for f in REQUIRED if not (paths.out / f).exists()]
    known_doi = {r[0] for r in con.execute("SELECT doi FROM papers WHERE doi IS NOT NULL")}
    for r in con.execute("SELECT COALESCE(version_note,'') || ' ' || COALESCE(crossref_json,'') FROM papers"):
        known_doi |= {d.lower().rstrip(".") for d in DOI_RE.findall(r[0])}   # 판본·정정 공지 DOI
    known_pid = {r[0] for r in con.execute("SELECT paper_id FROM papers")}
    known_eid = {r[0] for r in con.execute("SELECT evidence_id FROM evidence")}
    ghost = []
    for f in paths.out.glob("*.md"):
        if f.name.startswith("16_"):
            continue
        t = f.read_text(encoding="utf-8")
        for d in set(DOI_RE.findall(t)):
            if d.lower().rstrip(".") not in known_doi:
                ghost.append(f"{f.name}: DOI {d}")
        for pid in set(re.findall(r"P-\d{3,}", t)):
            if pid not in known_pid:
                ghost.append(f"{f.name}: {pid}")
        for eid in set(re.findall(r"E-\d{3,}-\d{2,}", t)):
            if eid not in known_eid:
                ghost.append(f"{f.name}: {eid}")
        if "DB에 없는 ID" in t:
            ghost.append(f"{f.name}: 렌더링 중 없는 ID 발견")
        if "의 근거 아님" in t:
            ghost.append(f"{f.name}: 다른 논문의 근거 ID를 인용")
    nocard = [r["paper_id"] for r in core if not con.execute("SELECT 1 FROM analysis_cards WHERE paper_id=?", (r["paper_id"],)).fetchone()]
    # 본문(09·11)에서 인용한 문헌이 Core·Extended(제외 아님)가 아니면 참고문헌에 빠진다(#12)
    from .render import CITE_BRACKET_RE, UNPARSED_CITE, parse_cite
    cited_out, unparsed = [], []
    for key in ("09", "11"):
        f = paths.state / "notes" / f"{key}_body.md"
        if not f.exists():
            continue
        for m in CITE_BRACKET_RE.finditer(f.read_text(encoding="utf-8")):
            groups = parse_cite(m.group(1))
            if groups is None:
                unparsed.append(f"{key}:{m.group(0)[:40]}")
                continue
            cited_out += [f"{key}:{g['pid']}" for g in groups if g["pid"] in known_pid and g["pid"] not in ids]
    for f in paths.out.glob("*.md"):
        if not f.name.startswith("16_") and UNPARSED_CITE in f.read_text(encoding="utf-8") and f"{f.name}" not in str(unparsed):
            unparsed.append(f"{f.name}: 렌더 결과에 해석 불가 인용")
    add("V15", "형식·완결성·허구 식별자", "FAIL" if ghost or cited_out else ("WARN" if missing or nocard or unparsed else "PASS"),
        f"누락 산출물 {missing}; 분석카드 없는 문헌 {nocard}; DB에 없는 DOI/ID {ghost[:20]}; "
        f"본문에서 인용했지만 Core·Extended가 아닌(제외·후보) 문헌 {cited_out}; 해석 불가 인용 {unparsed[:10]}")

    # V16 중요도 순위 재현성·타당성
    from . import ranking as RK
    v16_fail, v16_warn = [], []
    weights = {r["weight_version"]: json.loads(r["weights_json"]) for r in con.execute("SELECT * FROM ranking_weights")}
    cur = con.execute("SELECT * FROM paper_rankings WHERE is_current=1").fetchall()
    for wv, w in weights.items():   # 규칙 검사: 가중치 범위·합(#9)
        if any(not (0 <= float(x) <= 1) for x in w.values()) or abs(sum(w.values()) - 1) > 1e-6:
            v16_fail.append(f"가중치 {wv}가 범위(각 0~1, 합 1)를 벗어남: {w}")
    for scope in ("CORE", "EXTENDED"):
        pins = [r["pinned_rank"] for r in cur if r["set_scope"] == scope and r["pinned_rank"]]
        if len(pins) != len(set(pins)) or any(p < 1 for p in pins):
            v16_fail.append(f"{scope}: 같은 순위에 겹친 Pin 또는 1 미만 Pin {sorted(pins)}")
    for r in cur:
        if r["importance_score"] is not None and not (0 <= r["importance_score"] <= 100):
            v16_fail.append(f"{r['paper_id']}: 점수 {r['importance_score']}가 0~100 밖")
        w = weights.get(r["weight_version"])
        exp = RK.compute_score({c: r[c] for c in db.CRITERIA}, w) if w else "NO_WEIGHTS"
        if exp != r["importance_score"] and not (exp is not None and r["importance_score"] is not None and abs(exp - r["importance_score"]) < 0.01):
            v16_fail.append(f"{r['paper_id']}: 저장 점수 {r['importance_score']} ≠ 재계산 {exp}")
        why = json.loads(r["rationale_json"] or "{}")
        if any(r[c] is not None and not (why.get(c) or "").strip() for c in db.CRITERIA):
            v16_fail.append(f"{r['paper_id']}: 근거 없는 기준 점수")
        if re.search(r"피인용|인용\s*수|인용횟수|impact factor|\bIF\b|h-index|인용지수|citations?", json.dumps(why, ensure_ascii=False), re.I):
            v16_warn.append(f"{r['paper_id']}: 근거에 인용지표 언급 — 점수 근거로 쓰지 않았는지 확인")
        if r["importance_score"] is not None and r["importance_tier"] not in RK.TIERS:
            v16_warn.append(f"{r['paper_id']}: 등급 미지정")
        if r["pinned_rank"] and not (r["override_reason"] or "").strip():
            v16_fail.append(f"{r['paper_id']}: 사유 없는 순위 고정")
    for scope in ("CORE", "EXTENDED"):   # 정렬 재현: 저장 순위를 다시 계산해 비교
        rows = RK.current_rows(con, scope)
        before = {r["eval_id"]: (r["ai_rank"], r["core_rank"]) for r in rows}
        RK.recompute(con, scope)
        after = {r["eval_id"]: (r["ai_rank"], r["core_rank"]) for r in RK.current_rows(con, scope)}
        con.rollback()
        diff = [e for e in before if before[e] != after.get(e)]
        if diff:
            v16_fail.append(f"{scope}: 저장 순위와 재계산 순위 불일치 {len(diff)}건")
        core_ids = [r["paper_id"] for r in rows if r["core_rank"]]
        if len(core_ids) != len(set(core_ids)):
            v16_fail.append(f"{scope}: 같은 문헌이 중복 순위")
    ver = db.current_version(con)
    unranked = [r["paper_id"] for r in core if not con.execute("SELECT 1 FROM paper_rankings WHERE paper_id=? AND is_current=1 AND set_scope=?",
                                                                    (r["paper_id"], r["tier"])).fetchone()]
    stale = [r["paper_id"] for r in cur if r["version_id"] != ver]
    pending = [r["paper_id"] for r in cur if r["proposed_or_approved"] == "PENDING_EVIDENCE"]
    provisional = [r["paper_id"] for r in cur if RK.is_provisional(r)]
    if provisional:
        v16_warn.append(f"잠정 점수(근거 적절성 미평가) {provisional} — 원문 확인 후 재평가")
    if unranked:
        v16_warn.append(f"미평가 Core/Extended {unranked}")
    if stale:
        v16_warn.append(f"이전 연구계획 버전 기준 평가 {stale} — 재평가 제안 필요")
    if pending:
        v16_warn.append(f"평가보류 {pending}")
    if not (paths.out / "03a_논문별_중요도_순위표.md").exists() and cur:
        v16_warn.append("03a 미생성 — render 필요")
    add("V16", "중요도 순위 재현성·타당성", "FAIL" if v16_fail else ("WARN" if v16_warn or not cur else "PASS"),
        (f"현행 평가 {len(cur)}건; 문제 {v16_fail + v16_warn}" if cur else "중요도 평가 없음(rank score 미실행)")
        + ". 전문가 상위문헌 판정과의 일치는 사람 평가 필요")

    # V17 KCI/통합 검색·중복처리
    mode = db.search_mode(con)
    kci_runs = con.execute("SELECT COUNT(*) FROM search_runs WHERE db='KCI'").fetchone()[0]
    if mode == "INTERNATIONAL" and not kci_runs:
        add("V17", "KCI/통합 검색·중복처리", "N/A", "INTERNATIONAL 모드 — KCI 미사용(2차 기능)")
    else:
        v17_fail, v17_warn = [], []
        pf = paths.state / "kci_probe.json"
        probe = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else {}
        if not probe.get("verified"):
            v17_warn.append("KCI 실측 미통과 — KCI 응답 필드는 '미검증'")
        if not kci_runs:
            v17_warn.append(f"{mode} 모드지만 KCI 검색 기록 없음")
        rev = con.execute("SELECT COUNT(*) FROM duplicate_pairs WHERE status='REVIEW_DUPLICATE'").fetchone()[0]
        if rev:
            v17_warn.append(f"국내외 중복 검토 대기 {rev}건(dedup --review)")
        for r in con.execute("""SELECT a.paper_id x, b.paper_id y, a.year ya, b.year yb FROM papers a JOIN papers b
                                ON b.canonical_work_id=a.paper_id WHERE ABS(COALESCE(a.year,0)-COALESCE(b.year,0))>2"""):
            v17_fail.append(f"병합 의심: {r['x']}↔{r['y']} 연도 차 {abs((r['ya'] or 0) - (r['yb'] or 0))}")
        if mode == "KCI":
            mixed = [r for r in core if r["tier"] == "CORE" and (r["db_origin"] or "OPENALEX") == "OPENALEX"]
            no_consent = [r["paper_id"] for r in mixed if not (r["consent_note"] or "").strip()]
            if no_consent:   # 동의 문구가 DB에 저장된 경우만 인정(#7)
                v17_fail.append(f"KCI 모드 Core에 사용자 동의 기록 없는 해외 DB 문헌 {no_consent}")
            elif mixed:
                v17_warn.append(f"KCI 모드 Core에 해외 DB 문헌 {[r['paper_id'] for r in mixed]}(동의 기록 있음 — 03·보고서에 명시)")
        add("V17", "KCI/통합 검색·중복처리", "FAIL" if v17_fail else ("WARN" if v17_warn else "PASS"),
            f"모드 {mode}; KCI Run {kci_runs}; 문제 {v17_fail + v17_warn}. 병합 정확도는 사람 확인 필요")

    # 원본 불변
    if paths.raw_manifest.exists():
        before = json.loads(paths.raw_manifest.read_text(encoding="utf-8"))
        now_m = raw_manifest(paths)
        changed = [k for k in before if k in now_m and now_m[k] != before[k]]
        removed = [k for k in before if k not in now_m]
        add("RAW", "원본(raw/) 불변", "FAIL" if changed or removed else "PASS",
            f"변경 {changed}; 삭제 {removed}; 신규 {[k for k in now_m if k not in before]}")
    else:
        add("RAW", "원본(raw/) 불변", "WARN", "manifest 없음 — register-raw 미실행")

    human = [("V16", "중요도 상위문헌의 전문가 판정 일치"), ("V17", "KCI·해외 병합 정확도(사람 확인)"), ("V02", "Seed 역할 적합성"), ("V03", "Core 적합성(Precision@20)"), ("V05", "검색 포괄성(Recall·중요 누락, 정답 대조)"),
             ("V09", "품질 검토 적정성"), ("V10", "장절 매핑 유용성"), ("V11", "피드백 기반 확장"), ("V12", "검색 효율(우선 출처 사용/미사용 비교)·실무 시간 절감")]
    count = {k: [r[0] for r in res if r[2] == k] for k in ("FAIL", "WARN", "INSUFFICIENT", "N/A", "PASS")}
    headline = (f"FAIL {len(count['FAIL'])} · WARN {len(count['WARN'])} · INSUFFICIENT {len(count['INSUFFICIENT'])} "
                f"· N/A {len(count['N/A'])} · PASS {len(count['PASS'])} — 원문 확보 {ft_rate * 100:.0f}%, "
                f"직접 인용 원문 확인 {n_ok}/{n_quotes}")
    L = ["# 16 검증결과", "", f"**{headline}**", "", f"- 검증 일시: {db.now()}",
         "- 판정: PASS / WARN(사람 확인 필요) / FAIL(수정 필요) / INSUFFICIENT(검사 대상이 기준보다 적어 보증 불가) / N/A(검사 대상 없음)",
         "- **FAIL 0건이어도 INSUFFICIENT·N/A가 있으면 그 항목은 검증된 것이 아니다.**", "",
         "## 1. 자동 검증", "", "| ID | 항목 | 판정 | 내용 |", "|---|---|---|---|"]
    for i, n, v, d in res:
        L.append(f"| {i} | {n} | **{v}** | {str(d).replace('|', '/')} |")
    L += ["", "## 2. 사람 평가가 필요한 지표 (미검증)", "", "| ID | 항목 | 상태 |", "|---|---|---|"]
    L += [f"| {i} | {n} | 미검증 — Gold Standard·연구자 평가 필요 〔확인 필요〕 |" for i, n in human]
    fails = [r for r in res if r[2] == "FAIL"]
    L += ["", "## 3. 종합", "",
          f"- {headline}.",
          "- MVP 완료 판정에는 V01·V04·V05·V06·V07·V08·V13·V15·V16의 중대 오류 0건과 파일럿 실측이 필요하다. 실측 전 상태는 **미검증**.",
          "- KCI/통합 기능 완료는 V17(실제 API 검색·국내외 중복 검증) 통과가 추가로 필요하다(2차)."]
    summary = {"fail": count["FAIL"], "warn": count["WARN"], "insufficient": count["INSUFFICIENT"], "na": count["N/A"],
               "fulltext_rate": round(ft_rate, 2), "verified_quotes": f"{n_ok}/{n_quotes}"}
    return "\n".join(L) + "\n", summary
