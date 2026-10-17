"""최종 검증(V01·V04·V06·V07·V08·V13·V14·V15 + 원본 불변). 결과는 out/16_검증결과.md.
사람 평가가 필요한 지표(V02·V03·V05·V09·V10·V11·V12)는 '미검증'으로 남긴다 — 임의 합격 처리 금지."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import db
from .config import CORE_TARGET, INTEGRITY_STALE_DAYS, QUOTE_MAX_WORDS, Paths
from .fulltext import MIN_PDF_BYTES, TITLE_MATCH_MIN, sha256, verify_evidence_item, page_text
from .papers import norm_title
from .render import member_rows

REQUIRED = ["01_연구맥락_검색전략.md", "01a_우선학술출처_선정표.md", "03a_논문별_중요도_순위표.md", "02_Seed_3편_선정표.md", "03_핵심문헌_20편.csv", "04_검색_선별_이력.md",
            "05_원문확보_현황.csv", "06_원문다운로드_요청목록.md", "07_논문별_분석카드.md", "08_인용근거_라이브러리.md",
            "09_선행연구_비교분석.md", "10_연구계획_문헌활용계획.md", "11_선행연구_종합보고서.md", "12_연구공백_및_확장계획.md"]
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s|)\]<>`\"',;]+")
NUM_RE = re.compile(r"(?<![\w.,])[-−]?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|(?<![\w.,])[-−]?\d+(?:\.\d+)?%?")   # 천단위 쉼표 숫자를 하나로


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
    add("V01", "서지정보 정확성(Crossref 대조)", "FAIL" if bad else ("WARN" if unchecked else "PASS"),
        f"불일치 {len(bad)}건 {bad}; 미점검 {len(unchecked)}건 {unchecked[:10]}; DOI 없음 {len(nodoi)}건 {nodoi} (자동 제외하지 않음)")

    # V04 중복
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
        if t in seen_t:
            dups.append((seen_t[t], r["paper_id"]))
        seen_t[t] = r["paper_id"]
    linked_in = [r["paper_id"] for r in core if r["canonical_work_id"] and r["canonical_work_id"] in ids]
    n_runs = con.execute("SELECT COUNT(*) FROM search_runs").fetchone()[0]
    add("V04", "중복·선별 이력", "FAIL" if dups or linked_in else ("PASS" if n_runs else "WARN"),
        f"중복 의심 {dups}; 대표판과 함께 집합에 남은 판본 {linked_in}; 검색 Run {n_runs}건 기록")

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
    no_reason = [r["paper_id"] for r in outside_rows if not (r["m_reason"] or "").strip()]
    if no_reason:
        v5_warn.append(f"우선 출처 밖 문헌의 선정 사유 없음: {no_reason}")
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
    tried = con.execute("SELECT COUNT(*) FROM fulltexts").fetchone()[0]
    add("V06", "원문 확보 정확성", "FAIL" if issues else ("WARN" if undecided else "PASS"),
        (f"유효 전문 {n_full}편 / 시도 {tried}편 (전문 확보율 {n_full / tried * 100:.0f}%)" if tried else "확보 시도 없음")
        + (f"; 문제 {issues}" if issues else "")
        + (f"; 제목 판정 불가(사람 확인) {undecided}" if undecided else ""))

    # V07 직접 인용
    v7, long_q, changed = [], [], []
    for e in con.execute("SELECT * FROM evidence"):
        q = e["verbatim_short_quote"]
        if not q:
            continue
        if len(q.split()) > QUOTE_MAX_WORDS:
            long_q.append(e["evidence_id"])
        st, note = verify_evidence_item(con, paths, dict(e))
        if st != e["verification_status"]:
            changed.append(f"{e['evidence_id']}: 기록 {e['verification_status']} → 재검증 {st}")
        if st in ("UNVERIFIED", "CONFLICT"):
            v7.append(f"{e['evidence_id']}({st})")
    add("V07", "직접인용 정확성(원문 글자 대조)", "FAIL" if changed or long_q else ("WARN" if v7 else "PASS"),
        f"미검증/충돌 인용 {len(v7)}건 {v7[:15]} (보고서 사용 금지 대상); 상태 변동 {changed}; {QUOTE_MAX_WORDS}단어 초과 {long_q}")

    # V08 수치 대조(재서술 속 숫자가 인용문·해당 쪽에 있는지)
    v8 = []
    for e in con.execute("SELECT * FROM evidence WHERE type IN ('finding','statistic','result')"):
        nums = set(n.replace("−", "-") for n in NUM_RE.findall(e["paraphrase"] or "")) - {"1", "2", "3"}
        if not nums:
            continue
        src = (e["verbatim_short_quote"] or "")
        if e["pdf_page"] and db.fulltext_status(con, e["paper_id"]) in ("FULL_PDF", "FULL_XML"):
            for pg in (int(e["pdf_page"]) - 1, int(e["pdf_page"]), int(e["pdf_page"]) + 1):   # 표가 다음 쪽으로 넘어가는 경우
                src += " " + (page_text(paths, e["paper_id"], pg) or "") if pg > 0 else ""
        src = re.sub(r"[−–]\s*(?=\d)", "-", src).replace(",", "")   # 유니코드 마이너스·천단위 쉼표 정규화
        if not src.strip():
            p = con.execute("SELECT abstract FROM papers WHERE paper_id=?", (e["paper_id"],)).fetchone()
            src = (p["abstract"] or "") if p else ""
        missing = [n for n in nums if n.replace(",", "") not in src]
        if missing:
            v8.append(f"{e['evidence_id']}: {missing}")
    add("V08", "연구결과 수치 대조", "WARN" if v8 else "PASS",
        (f"원문/초록에서 찾지 못한 수치 {v8} — 사람이 확인" if v8 else "재서술의 수치가 모두 근거 텍스트에서 발견됨")
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
    add("V15", "형식·완결성·허구 식별자", "FAIL" if ghost else ("WARN" if missing or nocard else "PASS"),
        f"누락 산출물 {missing}; 분석카드 없는 문헌 {nocard}; DB에 없는 DOI/ID {ghost[:20]}")

    # V16 중요도 순위 재현성·타당성
    from . import ranking as RK
    v16_fail, v16_warn = [], []
    weights = {r["weight_version"]: json.loads(r["weights_json"]) for r in con.execute("SELECT * FROM ranking_weights")}
    cur = con.execute("SELECT * FROM paper_rankings WHERE is_current=1").fetchall()
    for r in cur:
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
        rev = con.execute("SELECT COUNT(DISTINCT paper_id) FROM paper_external_ids WHERE match_status='REVIEW_DUPLICATE'").fetchone()[0]
        if rev:
            v17_warn.append(f"국내외 중복 검토 대기 {rev}건(dedup --review)")
        for r in con.execute("""SELECT a.paper_id x, b.paper_id y, a.year ya, b.year yb FROM papers a JOIN papers b
                                ON b.canonical_work_id=a.paper_id WHERE ABS(COALESCE(a.year,0)-COALESCE(b.year,0))>2"""):
            v17_fail.append(f"병합 의심: {r['x']}↔{r['y']} 연도 차 {abs((r['ya'] or 0) - (r['yb'] or 0))}")
        if mode == "KCI":
            mixed = [r["paper_id"] for r in core if r["tier"] == "CORE" and (r["db_origin"] or "OPENALEX") == "OPENALEX"]
            if mixed:
                v17_warn.append(f"KCI 모드 Core에 해외 DB 문헌 {mixed} — 사용자 동의 기록 확인")
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
    L = ["# 16 검증결과", "", f"- 검증 일시: {db.now()}", "- 판정: PASS / WARN(사람 확인 필요) / FAIL(수정 필요)", "",
         "## 1. 자동 검증", "", "| ID | 항목 | 판정 | 내용 |", "|---|---|---|---|"]
    for i, n, v, d in res:
        L.append(f"| {i} | {n} | **{v}** | {str(d).replace('|', '/')} |")
    L += ["", "## 2. 사람 평가가 필요한 지표 (미검증)", "", "| ID | 항목 | 상태 |", "|---|---|---|"]
    L += [f"| {i} | {n} | 미검증 — Gold Standard·연구자 평가 필요 〔확인 필요〕 |" for i, n in human]
    fails = [r for r in res if r[2] == "FAIL"]
    L += ["", "## 3. 종합", "",
          f"- FAIL {len(fails)}건, WARN {sum(1 for r in res if r[2] == 'WARN')}건.",
          "- MVP 완료 판정에는 V01·V04·V05·V06·V07·V08·V13·V15·V16의 중대 오류 0건과 파일럿 실측이 필요하다. 실측 전 상태는 **미검증**.",
          "- KCI/통합 기능 완료는 V17(실제 API 검색·국내외 중복 검증) 통과가 추가로 필요하다(2차)."]
    summary = {"fail": [r[0] for r in fails], "warn": [r[0] for r in res if r[2] == "WARN"]}
    return "\n".join(L) + "\n", summary
