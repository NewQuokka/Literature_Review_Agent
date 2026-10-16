"""산출물(out/) 생성(F10). 표·목록은 DB에서 기계적으로 만들고,
서술(비교분석·종합보고서 본문)은 state/notes/의 초안을 조립하면서 인용 ID를 서지로 치환한다."""
from __future__ import annotations

import csv
import json
import re
from collections import Counter
from pathlib import Path

from . import db
from .config import CORE_TARGET, Paths

ROLE_KO = {"theory": "이론", "methodology": "방법론", "empirical": "유사 실증사례"}
PID_RE = re.compile(r"\[(P-\d{3,})(?:[,;]\s*(E-\d{3,}-\d{2,}(?:[,;]\s*E-\d{3,}-\d{2,})*))?\]")


def md_cell(s) -> str:
    if s is None or s == "":
        return "확인 필요"
    return str(s).replace("|", "\\|").replace("\n", " ")


def authors_list(p) -> list[dict]:
    cr = json.loads(p["crossref_json"] or "{}") if "crossref_json" in p.keys() else {}
    if cr.get("author"):
        return [{"family": a.get("family") or a.get("name") or "", "given": a.get("given") or ""} for a in cr["author"]]
    out = []
    for a in json.loads(p["authors_json"] or "[]"):
        parts = (a.get("name") or "").split()
        out.append({"family": parts[-1] if parts else "", "given": " ".join(parts[:-1])})
    return out


def short_cite(p) -> str:
    au = authors_list(p)
    yr = p["year"] or "n.d."
    if not au:
        return f"{(p['title'] or '')[:30]}, {yr}"
    if len(au) == 1:
        return f"{au[0]['family']}, {yr}"
    if len(au) == 2:
        return f"{au[0]['family']} & {au[1]['family']}, {yr}"
    return f"{au[0]['family']} et al., {yr}"


def apa(p) -> str:
    """APA 7판 근사 형식(최종 투고 전 서지관리도구로 재확인 권장)."""
    au = authors_list(p)

    def one(a):
        initials = " ".join(x[0] + "." for x in re.split(r"[\s\-]+", a["given"]) if x)
        return f"{a['family']}, {initials}".strip().rstrip(",")
    if not au:
        names = ""
    elif len(au) == 1:
        names = one(au[0])
    elif len(au) <= 20:
        names = ", ".join(one(a) for a in au[:-1]) + ", & " + one(au[-1])
    else:
        names = ", ".join(one(a) for a in au[:19]) + ", ... " + one(au[-1])
    cr = json.loads(p["crossref_json"] or "{}")
    venue = (cr.get("container-title") or [p["venue"] or ""])[0] if cr.get("container-title") else (p["venue"] or "")
    vol, iss, pg = cr.get("volume"), cr.get("issue"), cr.get("page")
    tail = f" *{venue}*" if venue else ""
    if vol:
        tail += f", *{vol}*" + (f"({iss})" if iss else "")
    if pg:
        tail += f", {pg}"
    doi = f" https://doi.org/{p['doi']}" if p["doi"] else (f" https://openalex.org/{p['openalex_id']}" if p["openalex_id"] else "")
    return f"{names} ({p['year'] or 'n.d.'}). {p['title']}.{tail}.{doi}".replace("..", ".")


def paper(con, pid):
    return con.execute("SELECT * FROM papers WHERE paper_id=?", (pid,)).fetchone()


def member_rows(con, tiers=("CORE", "EXTENDED"), include_rejected=False):
    q = f"""SELECT p.*, m.tier, m.status, m.reason AS m_reason FROM papers p JOIN paper_memberships m USING(paper_id)
            WHERE m.tier IN ({','.join('?' * len(tiers))})"""
    if not include_rejected:
        q += " AND m.status!='REJECTED'"
    return con.execute(q + " ORDER BY p.paper_id", tiers).fetchall()


def discovery_paths(con, pid) -> list[str]:
    """'[Stage] 경로' 목록. Stage A=우선 출처, B=제한 없는 인용망, C=독립·광역, U=사용자 추천."""
    out = []
    for e in con.execute("""SELECT e.source_paper_id, e.edge_type, e.run_id, s.* FROM discovery_edges e
                            LEFT JOIN search_runs s ON s.run_id=e.run_id WHERE e.target_paper_id=?""", (pid,)):
        st = db.run_stage(e) if e["kind"] else "B"
        out.append(f"[{st}] {e['edge_type']}←{e['source_paper_id']}({e['run_id']})")
    for h in con.execute("""SELECT s.* FROM search_hits h JOIN search_runs s USING(run_id)
                            WHERE h.paper_id=? AND s.kind IN ('search','user')""", (pid,)):
        out.append(f"[{db.run_stage(h)}] {h['kind']}:{h['purpose']}({h['run_id']})")
    if not out:
        r = con.execute("SELECT first_seen_run FROM papers WHERE paper_id=?", (pid,)).fetchone()
        if r and r[0]:
            out.append(r[0])
    return out


def notes(paths: Paths, key: str) -> str | None:
    f = paths.state / "notes" / f"{key}.md"
    return f.read_text(encoding="utf-8") if f.exists() else None


def cite_replace(con, text: str, cited: set) -> str:
    def rep(m):
        pid = m.group(1)
        p = paper(con, pid)
        if not p:
            return f"[{pid} — ⚠ DB에 없는 ID]"
        cited.add(pid)
        labels = []
        for eid in re.findall(r"E-\d{3,}-\d{2,}", m.group(2) or ""):
            row = con.execute("SELECT paper_id FROM evidence WHERE evidence_id=?", (eid,)).fetchone()
            if not row:
                labels.append(f"{eid} — ⚠ DB에 없는 ID")
            elif row[0] != pid:
                labels.append(f"{eid} — ⚠ {pid}의 근거 아님")
            else:
                labels.append(eid)
        ev = "; " + ", ".join(labels) if labels else ""
        return f"({short_cite(p)} [{pid}{ev}])"
    return PID_RE.sub(rep, text)


def latest_profile(con):
    r = con.execute("SELECT * FROM research_versions ORDER BY version_no DESC LIMIT 1").fetchone()
    return (r, json.loads(r["profile_json"])) if r else (None, None)


# ---------------- 개별 산출물 ----------------
def r01(con, paths: Paths):
    ver, prof = latest_profile(con)
    if not prof:
        return None
    L = [f"# 01 연구맥락·검색전략 (Research Profile {ver['version_id']})", "",
         f"- 원본: `{ver['source_file'] or '대화 입력'}` · 등록: {ver['uploaded_at']}",
         f"- 검색 모드: **{db.search_mode(con)}** (INTERNATIONAL / KCI / INTEGRATED — KCI·통합은 인증키 실측 후 활성화, 상세: 01b)",
         "- `미정` 항목은 입력에서 확인되지 않은 것으로, 에이전트가 임의로 확정하지 않았음.", ""]

    def lst(v):
        if not v:
            return "미정"
        if isinstance(v, list):
            return "<br>".join(json.dumps(x, ensure_ascii=False) if isinstance(x, dict) else str(x) for x in v)
        return str(v)
    L += ["## 1. Research Profile", "", "| 항목 | 내용 |", "|---|---|"]
    labels = [("topic", "연구주제"), ("research_fields", "주요 연구분야(이론/방법/실증)"),
              ("source_preferences", "사용자 선호·제외 학술출처"),
              ("research_questions", "연구질문"), ("hypotheses", "가설"),
              ("theories", "이론"), ("key_concepts", "핵심 개념"), ("variables", "변수"),
              ("population", "연구대상"), ("setting", "맥락·조건"), ("methods", "연구방법"), ("data", "데이터"),
              ("plan_sections", "연구계획 장·절"), ("undetermined", "미정·확인 필요")]
    for k, lab in labels:
        L.append(f"| {lab} | {md_cell(lst(prof.get(k)))} |")
    sc = prof.get("scope") or {}
    L += ["", "## 2. 검색범위·선정기준", "", "| 조건 | 값 |", "|---|---|",
          f"| 기간 | {md_cell(sc.get('years') or '제한 없음(사용자 미지정)')} |",
          f"| 언어 | {md_cell(sc.get('languages') or '제한 없음(사용자 미지정)')} |",
          f"| 분야 | {md_cell(sc.get('fields') or '제한 없음(사용자 미지정)')} |",
          f"| 포함 기준 | {md_cell(lst(sc.get('include')) if sc.get('include') else '연구질문 관련성(기본)')} |",
          f"| 제외 기준 | {md_cell(lst(sc.get('exclude')) if sc.get('exclude') else '사용자 지정 없음 — 임의 배제 안 함')} |", ""]
    ss = prof.get("search_strategy") or {}
    L += ["## 3. 검색전략(역할별 검색식)", "", "| 역할 | 검색식(외부 DB로 전송되는 문자열) |", "|---|---|"]
    for role in ("theory", "methodology", "empirical", "independent"):
        for q in ss.get(role) or []:
            L.append(f"| {ROLE_KO.get(role, '독립검색')} | `{md_cell(q)}` |")
    L += ["", "- 검색 DB: OpenAlex(주), Crossref(서지·출판상태 검증), Semantic Scholar(공개 PDF 보완)",
          "- 외부 전송 범위: 위 검색식·DOI만 전송. 연구계획서 원문은 전송하지 않음.",
          "", "## 4. Stage A/B/C 검색 프로토콜", "",
          "| Stage | 탐색 범위 | 규칙 | 전환 조건 |", "|---|---|---|---|",
          "| A 우선 탐색 | 분야별 우선 학술지·학회 5~10곳 내외(`01a`) | 검증된 Source ID 필터로 역할별 검색 → Seed 후보 | Seed 선정 후 B 필수. 결과 부족 시 출처 보완·제한 완화(사유 기록) |",
          "| B 인용망·의미 확장 | Seed·주요 후보의 참고·피인용·유사 논문 | **출처 제한 없음**. 분야 밖 방법론 원전·학회논문도 개별 적합성으로 검토 | C 필수 |",
          "| C 독립·광역 검증 | 우선 출처·Seed 인용망과 무관한 검색식·인접분야 | Core 수량을 채워도 **최소 1회** 제한 없는 검색, A/B 결과와 비교 | 중요 누락 시 확장, 충분하면 Core 제안 |",
          "", "- 우선 출처는 검색 순서용 가변 목록이며, '우선 출처 밖'은 제외 사유가 아니다."]
    n = notes(paths, "01")
    if n:
        L += ["", "## 5. 검토 메모", "", n]
    return "\n".join(L) + "\n"


def paper_stages(con, pid) -> list[str]:
    rows = con.execute("""SELECT s.* FROM search_hits h JOIN search_runs s USING(run_id) WHERE h.paper_id=?""", (pid,)).fetchall()
    return sorted({db.run_stage(r) for r in rows})


def r01a(con, paths):
    rows = con.execute("SELECT * FROM preferred_sources ORDER BY gap_id, version_id, role, priority").fetchall()
    if not rows:
        return None
    L = ["# 01a 분야별 우선 학술출처 선정표", "",
         "> 우선 출처는 **검색 순서를 정하는 가변 목록**이며 Core 선정의 배타적 조건이 아니다. Stage A에서만 출처 필터를 쓰고,",
         "> Stage B(인용망)·C(독립검색)는 출처 제한 없이 수행한다. `is_core`는 CWTS Core 목록 플래그(Web of Science Core Collection 아님),",
         "> `listed_in`은 외부 목록 등재 정보로, 어느 것도 개별 논문의 품질을 보증하지 않는다.", ""]
    pref = db.all_preferred_ids(con)
    core = member_rows(con)
    groups = {}
    for r in rows:
        groups.setdefault((r["gap_id"] or "", r["version_id"]), []).append(r)
    for (gap, ver), rs in groups.items():
        L += [f"## {'Gap ' + gap + ' 전용' if gap else '기본'} 목록 (연구계획 {ver})", "",
              "| 우선순위 | 출처 | Source ID | 유형 | 분야 | 역할 | Scope 적합성 | 목록 등재 근거(참고) | 선정 이유 | 제안 | 상태 | 승인 | Core 기여 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in rs:
            n_core = sum(1 for c in core if c["venue_source_id"] == r["source_id"])
            L.append(f"| {r['priority'] if r['priority'] is not None else '-'} | {md_cell(r['source_name'])} | {r['source_id']} | {r['source_type']} | "
                     f"{md_cell(r['field'])} | {ROLE_KO.get(r['role'], r['role'] or '-')} | {md_cell(r['scope_fit'])} | {md_cell(r['source_list_evidence'])} | "
                     f"{md_cell(r['selection_reason'])} | {r['origin']} | {r['status']} | {r['approval']} | {n_core}편 |")
        L.append("")
    outside = [c for c in core if c["venue_source_id"] not in pref]
    L += [f"- Core+Extended {len(core)}편 중 우선 출처 밖 문헌 **{len(outside)}편** — 상세 사유는 03 CSV `우선출처밖_선정이유`.",
          "- 사용자 제외 출처(USER_EXCLUDED)는 모든 Stage 검색에서 부정 필터로 제외된다."]
    n = notes(paths, "01a")
    if n:
        L += ["", "## 검토 메모", "", n]
    return "\n".join(L) + "\n"


# ---------------- 중요도 순위(v1.3) ----------------
TIER_KO = {"A": "A 핵심 활용", "B": "B 적극 참고", "C": "C 보완 참고", "평가보류": "평가보류"}
SHORT = {"research_fit": "관련성", "role_contribution": "역할", "evidence_quality": "근거", "distinctiveness": "차별성",
         "time_relevance": "시점"}
RANK_NOTE = ("> 중요도 순위는 **이 연구계획 버전에서의 상대적 활용 우선순위**다. 학술지·논문의 절대적 서열이나 품질 확정값이 아니며, "
             "인용수·학술지 지표·KCI 등재 상태는 점수에 반영하지 않는다(보조 표시만).")


def ranking(con, pid):
    return con.execute("SELECT * FROM paper_rankings WHERE paper_id=? AND is_current=1", (pid,)).fetchone()


def role_rank_text(rk) -> str:
    if not rk:
        return "-"
    parts = [f"{ {'theory': '이론', 'methodology': '방법', 'empirical': '실증'}[r]} {rk[r + '_rank']}" for r in ("theory", "methodology", "empirical")
             if rk[r + "_rank"]]
    return ", ".join(parts) or "-"


def aux_indicators(r) -> str:
    """보조 정보 — DB별로 분리 표기, 합산·비교 금지."""
    parts = []
    if r["cited_by_count"] is not None and (r["db_origin"] or "OPENALEX") != "KCI":
        parts.append(f"OpenAlex 피인용 {r['cited_by_count']}")
    if r["kci_citation_count"] is not None:
        parts.append(f"KCI 피인용 {r['kci_citation_count']}")
    if r["wos_citation_count"] is not None:
        parts.append(f"WoS 피인용(KCI 제공) {r['wos_citation_count']}")
    return " / ".join(parts) or "-"


def ranking_table(con, scope="CORE", top: int | None = None) -> list[str]:
    rows = con.execute("""SELECT r.*, p.role_tags, p.title FROM paper_rankings r JOIN papers p USING(paper_id)
                          JOIN paper_memberships m ON m.paper_id=r.paper_id
                          WHERE r.is_current=1 AND r.set_scope=? AND m.tier=? AND m.status!='REJECTED'
                          ORDER BY r.core_rank IS NULL, r.core_rank, r.paper_id""", (scope, scope)).fetchall()
    if top:
        rows = rows[:top]
    maps = {}
    for m in con.execute("SELECT paper_id, section_id, section_title FROM section_mappings"):
        maps.setdefault(m["paper_id"], []).append(f"{m['section_id']} {m['section_title'] or ''}".strip())
    L = [f"| {'Core' if scope == 'CORE' else 'Extended'} 순위 | 역할별 순위 | P-ID | 문헌 | 역할 | 점수/등급 | 중요도 판단 근거(요약) | 활용할 장·절 | 원문·검증 상태 | 선정/수정 상태 |",
         "|---:|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        p = paper(con, r["paper_id"])
        why = json.loads(r["rationale_json"] or "{}")
        summ = "; ".join(f"{SHORT[c]}: {why[c]}" for c in ("research_fit", "role_contribution") if why.get(c))
        score = f"{r['importance_score']:.1f} / {TIER_KO.get(r['importance_tier'], r['importance_tier'] or '등급 미지정')}" \
            if r["importance_score"] is not None else "평가보류"
        state = f"원문 {db.fulltext_status(con, r['paper_id'])}, 근거 {r['basis']}, 출판 {db.integrity_status(con, r['paper_id'])}"
        status = r["proposed_or_approved"] + (f"(AI 제안 {r['ai_rank']}위 → 사용자 고정: {r['override_reason']})" if r["pinned_rank"] else "")
        L.append(f"| {r['rank_label'] or '-'} | {role_rank_text(r)} | {r['paper_id']} | {md_cell(short_cite(p))} | "
                 f"{md_cell(', '.join(ROLE_KO.get(t, t) for t in (r['role_tags'] or '').split(',') if t))} | {score} | {md_cell(summ)} | "
                 f"{md_cell(', '.join(maps.get(r['paper_id'], [])) or '미매핑')} | {state} | {status} |")
    return L


def r03a(con, paths):
    if not con.execute("SELECT 1 FROM paper_rankings").fetchone():
        return None
    ver = db.current_version(con)
    wv, w = db.current_weights(con)
    L = ["# 03a 논문별 중요도 순위표", "", RANK_NOTE, "",
         f"- 기준 연구계획: **{ver}** · 가중치 **{wv}** · 산식: 점수 = Σ(가중치 × 기준점수 0~5) × 20 (0~100)",
         "- 동점 규칙: 연구질문 직접 관련성 → 역할 기여도 → 근거 적절성 → 사용자 승인. 그래도 같으면 공동 순위(`3=`).",
         "- 등급 A/B/C는 고정 점수구간이 아니라 점수 분포·역할·근거를 보고 부여한 제안이다. 근거가 없는 기준이 있으면 `평가보류`.", "",
         "| 기준 | 가중치 |", "|---|---:|"]
    L += [f"| {db.CRITERIA_KO[c]} | {w[c] * 100:.0f}% |" for c in db.CRITERIA]
    L += ["", "## 1. Core 전체 순위", ""] + ranking_table(con, "CORE")
    for role in db.ROLES:
        rows = con.execute(f"""SELECT r.* FROM paper_rankings r JOIN paper_memberships m ON m.paper_id=r.paper_id
                               WHERE r.is_current=1 AND r.set_scope='CORE' AND m.tier='CORE' AND m.status!='REJECTED'
                               AND r.{role}_rank IS NOT NULL ORDER BY r.{role}_rank""").fetchall()
        L += ["", f"## 2-{db.ROLES.index(role) + 1}. {ROLE_KO[role]} 유형 순위", "",
              "| 순위 | P-ID | 문헌 | 점수 | 전체 순위 |", "|---:|---|---|---|---|"]
        L += [f"| {r[role + '_rank']} | {r['paper_id']} | {md_cell(short_cite(paper(con, r['paper_id'])))} | {r['importance_score']} | {r['rank_label']} |"
              for r in rows] or ["| - | 해당 역할 문헌 없음 | | | |"]
    if con.execute("SELECT 1 FROM paper_rankings WHERE is_current=1 AND set_scope='EXTENDED'").fetchone():
        L += ["", "## 3. Extended 집합 내 순위(Core와 별도)", ""] + ranking_table(con, "EXTENDED")
    unranked = [r["paper_id"] for r in member_rows(con) if not ranking(con, r["paper_id"])]
    stale = [r["paper_id"] for r in con.execute("SELECT paper_id FROM paper_rankings WHERE is_current=1 AND version_id!=?", (ver,))]
    L += ["", "## 4. 확인 필요", "",
          f"- 아직 평가하지 않은 Core/Extended 문헌: {', '.join(unranked) or '없음'}",
          f"- 이전 연구계획 버전 기준 평가(재평가 필요): {', '.join(stale) or '없음'}"]
    L += ["", "## 부록 F. 기준별 상세 점수·근거", "",
          "| P-ID | " + " | ".join(db.CRITERIA_KO[c] for c in db.CRITERIA) + " | 점수 | 분석 기반 | 불확실성 | 보조 정보(순위 미반영) | 평가 | 버전·가중치 |",
          "|---|" + "---|" * len(db.CRITERIA) + "---|---|---|---|---|---|"]
    for r in con.execute("SELECT * FROM paper_rankings WHERE is_current=1 ORDER BY set_scope, core_rank IS NULL, core_rank"):
        why = json.loads(r["rationale_json"] or "{}")
        cells = [f"{r[c] if r[c] is not None else '보류'} — {md_cell(why.get(c) or '근거 없음')}" for c in db.CRITERIA]
        L.append(f"| {r['paper_id']} | " + " | ".join(cells) +
                 f" | {r['importance_score'] if r['importance_score'] is not None else '평가보류'} | {r['basis']} | {md_cell(r['uncertainty'] or '-')} | "
                 f"{aux_indicators(paper(con, r['paper_id']))} | {r['evaluated_by']} {r['evaluated_at']} | {r['version_id']}·{r['weight_version']} |")
    hist = con.execute("""SELECT o.paper_id, o.importance_score AS s0, o.core_rank AS r0, o.version_id AS v0, o.weight_version AS w0,
                                 n.importance_score AS s1, n.core_rank AS r1, n.version_id AS v1, n.weight_version AS w1,
                                 n.change_reason, n.evaluated_at
                          FROM paper_rankings n JOIN paper_rankings o ON o.paper_id=n.paper_id AND o.eval_id=(
                               SELECT MAX(eval_id) FROM paper_rankings x WHERE x.paper_id=n.paper_id AND x.eval_id<n.eval_id)
                          ORDER BY n.eval_id""").fetchall()
    if hist:
        L += ["", "## 부록 F-2. 순위·점수 변경 이력", "", "| 일시 | P-ID | 이전(점수/순위/버전) | 이후(점수/순위/버전) | 변경 사유 |", "|---|---|---|---|---|"]
        L += [f"| {h['evaluated_at']} | {h['paper_id']} | {h['s0']} / {h['r0'] or '-'} / {h['v0']}·{h['w0']} | "
              f"{h['s1']} / {h['r1'] or '-'} / {h['v1']}·{h['w1']} | {md_cell(h['change_reason'])} |" for h in hist]
    n = notes(paths, "03a")
    if n:
        L += ["", "## 검토 메모", "", n]
    return "\n".join(L) + "\n"


def r01b(con, paths):
    mode = db.search_mode(con)
    kci_runs = con.execute("SELECT COUNT(*) FROM search_runs WHERE db='KCI'").fetchone()[0]
    if mode == "INTERNATIONAL" and not kci_runs:
        return None
    st = json.loads(con.execute("SELECT mode_settings FROM projects").fetchone()[0] or "{}")
    pf = paths.state / "kci_probe.json"
    probe = json.loads(pf.read_text(encoding="utf-8")) if pf.exists() else {}
    L = ["# 01b 국내외 검색모드·통합 이력", "",
         f"- 현재 모드: **{mode}** · 국내외 비중 메모: {st.get('domestic_share') or '지정 없음(연구목적에 따라 유동적, 고정 비율 아님)'}",
         f"- KCI Open API 실측: {'통과' if probe.get('verified') else '미통과/미실행'} ({probe.get('probed_at', '-')})"
         + (f" · 키워드 단독 검색 {'지원' if probe.get('keyword_only_supported') else '미지원(제목 필수)'}" if probe else ""),
         "- KCI 학술지 인용지수는 논문 인용수가 아니며, KCI·해외 DB 인용수는 서로 비교·합산하지 않는다.", "",
         "## 1. 모드 변경 이력", "", "| 일시 | 이전 | 이후 | 사유 | 연구계획 버전 |", "|---|---|---|---|---|"]
    L += [f"| {h['at']} | {h['from']} | {h['to']} | {md_cell(h.get('reason'))} | {h.get('version')} |" for h in st.get("history", [])] or ["| - | - | - | - | - |"]
    L += ["", "## 2. DB·언어별 검색", "", "| DB | 언어 | Stage | Run | 반환 | 신규 |", "|---|---|---|---|---|---|"]
    agg = {}
    for r in con.execute("SELECT * FROM search_runs"):
        k = (r["db"], r["language"] or "-", db.run_stage(r))
        a = agg.setdefault(k, [0, 0, 0])
        a[0] += 1; a[1] += r["returned"] or 0; a[2] += r["new_papers"] or 0
    L += [f"| {k[0]} | {k[1]} | {k[2]} | {v[0]} | {v[1]} | {v[2]} |" for k, v in sorted(agg.items())]
    core = member_rows(con)
    origin = Counter((r["db_origin"] or "OPENALEX") for r in core)
    L += ["", "## 3. Core/Extended 국내외 구성", "",
          f"- KCI 단독 {origin.get('KCI', 0)}편 · 해외 DB 단독 {origin.get('OPENALEX', 0)}편 · 양쪽에서 확인 {origin.get('BOTH', 0)}편"]
    rows = con.execute("SELECT * FROM paper_external_ids WHERE match_status IN ('REVIEW_DUPLICATE','DISTINCT') ORDER BY paper_id").fetchall()
    L += ["", "## 4. 국내외 중복 검토", "", "| P-ID | 관련 P-ID | 유사도 | 상태 | 메모 |", "|---|---|---|---|---|"]
    seen = set()
    for r in rows:
        k = (r["paper_id"], r["related_paper_id"], r["match_status"])
        if k in seen:
            continue
        seen.add(k)
        L.append(f"| {r['paper_id']} | {r['related_paper_id'] or '-'} | {r['match_confidence']} | {r['match_status']} | {md_cell(r['note'])} |")
    if not rows:
        L.append("| - | - | - | 검토 대상 없음 | |")
    n = notes(paths, "01b")
    L += ["", "## 5. 모드별 편향 점검", "", n or "- (에이전트 메모 미작성) 한국어 논문의 영어 검색 누락, KCI만으로 국제 이론·방법론 원전이 빠질 가능성 등을 기록"]
    return "\n".join(L) + "\n"


def r02(con, paths):
    rows = con.execute("SELECT * FROM seeds ORDER BY CASE role WHEN 'theory' THEN 1 WHEN 'methodology' THEN 2 ELSE 3 END").fetchall()
    L = ["# 02 Seed 3편 선정표", "",
         "> 역할별 대표 문헌 1편. 승인 상태가 `PROPOSED`인 동안은 연구자 검토 전 **임시선정**이다.", "",
         "| 역할 | P-ID | 문헌 | 선정 근거 | 대안 후보 | 원문 | 승인 상태 |", "|---|---|---|---|---|---|---|"]
    for role in ("theory", "methodology", "empirical"):
        rs = [r for r in rows if r["role"] == role and r["status"] != "REJECTED"]
        if not rs:
            L.append(f"| {ROLE_KO[role]} | — | **없음 / 추가 탐색 필요** | — | — | — | — |")
        for r in rs:
            p = paper(con, r["paper_id"])
            alts = ", ".join(f"{a} {short_cite(paper(con, a))}" for a in (r["alternatives"] or "").split(",") if a and paper(con, a))
            st = r["status"] + (" (임시선정)" if r["provisional"] and r["status"] == "PROPOSED" else "")
            L.append(f"| {ROLE_KO[role]} | {r['paper_id']} | {md_cell(short_cite(p))} — {md_cell(p['title'])} | "
                     f"{md_cell(r['reason'])} | {md_cell(alts or '없음')} | {db.fulltext_status(con, r['paper_id'])} | {st} |")
    rej = [r for r in rows if r["status"] == "REJECTED"]
    if rej:
        L += ["", "## 교체·제외 이력", "", "| 역할 | P-ID | 사유 |", "|---|---|---|"]
        L += [f"| {ROLE_KO[r['role']]} | {r['paper_id']} | {md_cell(r['reason'])} |" for r in rej]
    n = notes(paths, "02")
    if n:
        L += ["", "## 검토 메모", "", n]
    return "\n".join(L) + "\n"


def r03(con, paths):
    f = paths.out / "03_핵심문헌_20편.csv"
    with open(f, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["paper_id", "구분", "승인상태", "중요도순위", "역할별순위", "중요도점수", "중요도등급", "순위상태",
                    "중요도근거(관련성·역할)", "역할", "Seed", "저자연도", "제목", "국문제목", "영문제목", "연도", "검색DB", "KCI_ID",
                    "출처(학술지·학회)",
                    "출처ID", "출처유형", "우선출처여부", "발견Stage", "DOI", "OpenAlex", "판본연결", "탐색경로", "선정사유",
                    "우선출처밖_선정이유", "원문상태", "서지검증", "출판상태", "피인용(보조·DB별, 순위 미반영)"])
        pref = db.all_preferred_ids(con)
        for r in member_rows(con, include_rejected=True):
            seed = con.execute("SELECT role FROM seeds WHERE paper_id=? AND status!='REJECTED'", (r["paper_id"],)).fetchone()
            in_pref = r["venue_source_id"] in pref
            rk = ranking(con, r["paper_id"])
            why = json.loads(rk["rationale_json"] or "{}") if rk else {}
            w.writerow([r["paper_id"], r["tier"], r["status"],
                        rk["rank_label"] if rk else "미평가", role_rank_text(rk),
                        rk["importance_score"] if rk and rk["importance_score"] is not None else "",
                        (rk["importance_tier"] or "") if rk else "", rk["proposed_or_approved"] if rk else "",
                        " / ".join(why.get(c, "") for c in ("research_fit", "role_contribution") if why.get(c)),
                        r["role_tags"], seed[0] if seed else "",
                        short_cite(r), r["title"], r["title_ko"] or "", r["title_en"] or "", r["year"],
                        r["db_origin"] or "OPENALEX", r["kci_id"] or "", r["venue"], r["venue_source_id"] or "", r["venue_type"] or "",
                        "Y" if in_pref else "N", "".join(paper_stages(con, r["paper_id"])), r["doi"], r["openalex_id"],
                        r["canonical_work_id"] or "", " | ".join(discovery_paths(con, r["paper_id"])),
                        r["m_reason"], "" if in_pref else (r["m_reason"] or "확인 필요"),
                        db.fulltext_status(con, r["paper_id"]), r["bib_match"] or "NOT_CHECKED",
                        db.integrity_status(con, r["paper_id"]), aux_indicators(r)])
    return None  # 직접 기록


def r04(con, paths):
    runs = con.execute("SELECT * FROM search_runs ORDER BY run_id").fetchall()
    L = ["# 04 검색·선별 이력 및 커버리지 점검", "",
         "> PRISMA-S의 검색기록 원칙을 참고한 재현용 로그. 정식 체계적 문헌고찰(Systematic Review)을 수행했다는 뜻이 아님.", "",
         "> Stage A = 우선 학술출처 제한 검색 / B = 출처 제한 없는 인용망·의미 확장 / C = 독립·광역 검증 / U = 사용자 추천.", "",
         "## 1. 검색 실행 기록", "",
         "| Run | Stage | DB | 일시 | 유형 | 목적 | 검색식/부모 | 출처 필터 | 기타 필터 | 범위확대 사유 | 전체 | 반환 | 신규 | 호출 | 비용USD | 버전 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        q = r["raw_query"] or ""
        if r["parent_paper_id"]:
            q = f"{r['parent_paper_id']} {q}".strip()
        L.append(f"| {r['run_id']} | {db.run_stage(r)} | {r['db']} | {r['run_date']} | {r['kind']} | {md_cell(r['purpose'])} | `{md_cell(q)}` | "
                 f"{md_cell(r['source_ids'] or '없음')} | {md_cell(r['filters'] or '-')} | {md_cell(r['scope_expansion_reason'] or '-')} | "
                 f"{r['total_count'] if r['total_count'] is not None else '-'} | {r['returned']} | {r['new_papers']} | "
                 f"{r['api_calls'] if r['api_calls'] is not None else '-'} | {r['cost_usd'] if r['cost_usd'] is not None else '-'} | {r['version_id']} |")
    st_sum = {}
    for r in runs:
        d = st_sum.setdefault(db.run_stage(r), [0, 0, 0, 0, 0.0])
        d[0] += 1; d[1] += r["returned"] or 0; d[2] += r["new_papers"] or 0
        d[3] += r["api_calls"] or 0; d[4] += r["cost_usd"] or 0
    L += ["", "### Stage별 요약(호출량·후보 수)", "", "| Stage | Run | 반환 | 신규 | API 호출 | 비용USD |", "|---|---|---|---|---|---|"]
    L += [f"| {k} | {v[0]} | {v[1]} | {v[2]} | {v[3]} | {round(v[4], 4)} |" for k, v in sorted(st_sum.items())]
    L += ["", "- 캐시 적중 요청은 호출·비용 0으로 기록된다. 우선 출처 검색이 비용을 줄였는지는 비제한 검색과 비교해 판단한다(단정하지 않음)."]
    tiers = Counter(r[0] for r in con.execute("SELECT tier || ':' || status FROM paper_memberships"))
    total = con.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    linked = con.execute("SELECT COUNT(*) FROM papers WHERE canonical_work_id IS NOT NULL").fetchone()[0]
    L += ["", "## 2. 선별 흐름", "", f"- 수집된 고유 레코드: **{total}건** (판본 연결로 통합된 레코드 {linked}건 포함)",
          "- 구분·상태별: " + ", ".join(f"{k} {v}" for k, v in sorted(tiers.items()))]
    dec = con.execute("SELECT decision, COUNT(*) c FROM screening_decisions GROUP BY decision").fetchall()
    if dec:
        L.append("- 선별 결정: " + ", ".join(f"{d['decision']} {d['c']}" for d in dec))
    L += ["", "### 제외·보류 사유", "", "| P-ID | 문헌 | 결정 | 사유 | 일시 |", "|---|---|---|---|---|"]
    for d in con.execute("SELECT * FROM screening_decisions WHERE decision IN ('exclude','hold') ORDER BY id"):
        p = paper(con, d["paper_id"])
        L.append(f"| {d['paper_id']} | {md_cell(short_cite(p))} | {d['decision']} | {md_cell(d['reason'])} | {d['decided_at']} |")
    core = member_rows(con)
    L += ["", "## 3. 커버리지 점검(Core+Extended, 제외 제외)", ""]
    if core:
        roles = Counter()
        for r in core:
            for t in (r["role_tags"] or "").split(","):
                if t:
                    roles[t] += 1
        years = Counter(((r["year"] or 0) // 5) * 5 for r in core)
        types = Counter(r["work_type"] or "미상" for r in core)
        langs = Counter(r["language"] or "미상" for r in core)
        venues = Counter(r["venue"] or "미상" for r in core)
        via = Counter()
        for r in core:
            kinds = {x.split("] ", 1)[-1].split("←")[0].split("(")[0].split(":")[0] for x in discovery_paths(con, r["paper_id"])}
            for k in kinds:
                via[k] += 1
        pref = db.all_preferred_ids(con)
        stage_sets = {r["paper_id"]: paper_stages(con, r["paper_id"]) for r in core}
        outside = [r["paper_id"] for r in core if r["venue_source_id"] not in pref]
        not_a = [p for p, s in stage_sets.items() if s and "A" not in s]
        c_only = [p for p, s in stage_sets.items() if s == ["C"]]
        L += [f"- 역할 태그(복수 허용): " + ", ".join(f"{ROLE_KO.get(k, k)} {v}" for k, v in roles.items()),
              f"- 발행연도(5년 구간): " + ", ".join(f"{k}–{k + 4}: {v}" for k, v in sorted(years.items())),
              f"- 문헌 유형: " + ", ".join(f"{k} {v}" for k, v in types.most_common()),
              f"- 언어: " + ", ".join(f"{k} {v}" for k, v in langs.most_common()),
              f"- 탐색 경로(논문당 복수): " + ", ".join(f"{k} {v}" for k, v in via.most_common()),
              f"- 동일 학술지 최다: " + ", ".join(f"{k} {v}" for k, v in venues.most_common(3)),
              "", "| 점검 항목 | 자동 관찰 |", "|---|---|"]
        n_core = sum(1 for r in core if r["tier"] == "CORE")
        L.append(f"| Core 목표 대비 | {n_core}/{CORE_TARGET}편" + (f" — **{CORE_TARGET - n_core}편 미충족** (억지 충원하지 않음)" if n_core < CORE_TARGET else "") + " |")
        L.append(f"| Stage별 발견(복수) | A {sum('A' in s for s in stage_sets.values())}편 · B {sum('B' in s for s in stage_sets.values())}편 · "
                 f"C {sum('C' in s for s in stage_sets.values())}편 · U {sum('U' in s for s in stage_sets.values())}편 |")
        L.append(f"| 우선 출처 밖 문헌 | {len(outside)}편 {outside} |")
        L.append(f"| Stage A에서 발견되지 않은 문헌(우선 출처 밖 유효 발견) | {len(not_a)}/{len(core)}편 {not_a} |")
        L.append(f"| 독립검색(C)에서만 발견 | {len(c_only)}편 {c_only} — 우선 출처·인용망이 놓친 영역 신호 |")
        n_c = sum(1 for r in runs if db.run_stage(r) == "C")
        L.append(f"| Stage C 실행 | {n_c}회" + (" — **미실행: 최소 1회 필수**" if not n_c else "") + " |")
        if langs and len(langs) == 1:
            L.append(f"| 언어 편중 | 전부 `{next(iter(langs))}` — 국내(한국어) 문헌 누락 가능성 검토 필요 |")
        recent = sum(1 for r in core if (r["year"] or 0) >= 2021)
        L.append(f"| 최신성 | 2021년 이후 {recent}편 |")
    checks = con.execute("SELECT * FROM search_coverage_checks ORDER BY check_id").fetchall()
    if checks:
        L += ["", "### 커버리지 점검 기록", "", "| ID | 일시 | B 경유 | C 경유 | 우선출처 밖 | A 미발견 | 중요 누락 | 결정 |", "|---|---|---|---|---|---|---|---|"]
        for c in checks:
            L.append(f"| {c['check_id']} | {c['created_at']} | {c['seed_network_hits']} | {c['independent_hits']} | "
                     f"{c['outside_source_hits']} | {md_cell(c['only_bc_relevant'] or '-')} | {md_cell(c['important_missed_papers'] or '-')} | {md_cell(c['decision'] or '-')} |")
    n = notes(paths, "04")
    if n:
        L += ["", "## 4. 누락 가능 영역(에이전트 검토 메모)", "", n]
    return "\n".join(L) + "\n"


def r05(con, paths):
    f = paths.out / "05_원문확보_현황.csv"
    with open(f, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["paper_id", "구분", "저자연도", "원문상태", "파일경로", "출처URL", "출처유형", "판본", "라이선스",
                    "쪽수", "제목일치도", "SHA256", "확보일시"])
        for r in member_rows(con):
            ft = con.execute("SELECT * FROM fulltexts WHERE paper_id=?", (r["paper_id"],)).fetchone()
            if ft:
                rel = str(Path(ft["file_path"]).relative_to(paths.root)) if ft["file_path"] and str(paths.root) in ft["file_path"] else ft["file_path"]
                w.writerow([r["paper_id"], r["tier"], short_cite(r), ft["access_status"], rel or "", ft["source_url"] or "",
                            ft["source_type"] or "", ft["version"] or "", ft["license"] or "미표시", ft["pages"] or "",
                            ft["title_match"] or "", ft["checksum"] or "", ft["acquired_at"]])
            else:
                w.writerow([r["paper_id"], r["tier"], short_cite(r), "NOT_CHECKED"] + [""] * 9)
    return None


def r06(con, paths):
    L = ["# 06 원문 다운로드 요청목록", "",
         "> 공개 원문을 자동으로 확보하지 못한 문헌. 기관 구독 등 **정당한 권한으로** 받은 PDF를 `raw/fulltext/`에 넣고",
         "> 파일명에 P-ID를 포함하면(예: `P-007.pdf`) 에이전트가 매칭해 `FULL_PDF`로 승격하고 근거를 재검증합니다.", "",
         "| P-ID | 구분 | 문헌 | DOI / 접근 페이지 | 미확보 사유 | 현재 분석 수준 | 사용자 할 일 |", "|---|---|---|---|---|---|---|"]
    n = 0
    for r in member_rows(con):
        ft = con.execute("SELECT * FROM fulltexts WHERE paper_id=?", (r["paper_id"],)).fetchone()
        st = ft["access_status"] if ft else "NOT_CHECKED"
        if st in ("FULL_PDF", "FULL_XML"):
            continue
        n += 1
        info = json.loads(ft["attempts_json"] or "{}") if ft else {}
        att = info.get("attempts", []) if isinstance(info, dict) else []
        reason = "; ".join(f"{a.get('kind')}: {a.get('result')}" for a in att) or (
            "공개(OA) PDF 위치 없음" if ft else "아직 확보 시도 전")
        link = f"https://doi.org/{r['doi']}" if r["doi"] else ""
        lps = info.get("landing_pages", []) if isinstance(info, dict) else []
        if lps:
            link += ("<br>" if link else "") + "공개본 페이지: " + lps[0]
        level = "초록 기반(ABSTRACT_BASED)" if r["abstract"] else "서지정보만(METADATA_ONLY)"
        todo = "공개본 페이지에서 직접 받아 업로드" if lps else "기관 구독/저자 요청으로 확보 후 업로드(불가 시 초록 기반 유지)"
        L.append(f"| {r['paper_id']} | {r['tier']} | {md_cell(short_cite(r))} — {md_cell(r['title'])} | {md_cell(link)} | "
                 f"{md_cell(reason)} | {level} | {todo} |")
    if n == 0:
        L.append("| — | — | 요청할 문헌 없음 | — | — | — | — |")
    L += ["", f"총 {n}편. 미확보 문헌은 목록에 유지하되 직접 인용·쪽수·상세 수치를 생성하지 않습니다."]
    return "\n".join(L) + "\n"


CARD_FIELDS = [("research_overview", "Research Overview (연구목적·질문·이론·가설)"),
               ("methodology", "Methodology (연구유형·표본·기간·데이터·변수·측정·분석·식별전략)"),
               ("key_findings", "Key Findings (방향·효과크기·유의성·조건, 근거 ID)"),
               ("quality_limitations", "Quality & Limitations"),
               ("citation_candidates", "Citation Candidates"),
               ("relevance", "Relevance (내 연구의 장·절·가설·변수 연결)"),
               ("open_questions", "Open Questions")]


def _fmt(v):
    if v is None or v == "" or v == []:
        return "원문에서 확인되지 않음"
    if isinstance(v, list):
        return "\n".join(f"- {_fmt(x) if not isinstance(x, str) else x}" for x in v)
    if isinstance(v, dict):
        return "\n".join(f"- **{k}**: {_fmt(x) if not isinstance(x, str) else x}" for k, x in v.items())
    return str(v)


def r07(con, paths):
    L = ["# 07 논문별 분석카드", "", "> 분석 기반(전문/초록)을 카드마다 표시. 초록 기반 카드에는 직접 인용·쪽수·표 수치가 없다.", ""]
    for r in member_rows(con):
        c = con.execute("SELECT * FROM analysis_cards WHERE paper_id=?", (r["paper_id"],)).fetchone()
        q = con.execute("SELECT * FROM quality_checks WHERE paper_id=?", (r["paper_id"],)).fetchone()
        card = json.loads(c["card_json"]) if c else {}
        fts = db.fulltext_status(con, r["paper_id"])
        basis = "전문(Full text)" if fts in ("FULL_PDF", "FULL_XML") else ("초록 기반(Abstract-based)" if r["abstract"] else "서지정보만")
        L += [f"## {r['paper_id']} — {short_cite(r)}", "",
              "| 항목 | 내용 |", "|---|---|",
              f"| 논문명 | {md_cell(r['title'])}" + (f"<br>영문: {md_cell(r['title_en'])}" if r['title_en'] and r['title_en'] != r['title'] else "") + " |",
              f"| 저자·연도·출처 | {md_cell(', '.join(a['family'] for a in authors_list(r)[:6]))} ({r['year']}), {md_cell(r['venue'])} |",
              f"| DOI·링크 | {('https://doi.org/' + r['doi']) if r['doi'] else ('https://openalex.org/' + r['openalex_id'] if r['openalex_id'] else '-')}"
              + (f" · KCI {r['kci_id']}" if r['kci_id'] else "") + f" · 검색DB {r['db_origin'] or 'OPENALEX'} |",
              f"| 문헌 역할 | {md_cell(', '.join(ROLE_KO.get(t, t) for t in (r['role_tags'] or '').split(',') if t))} |",
              f"| Selection | {r['tier']} / {r['status']} — {md_cell(r['m_reason'])}<br>출처: {md_cell(r['venue'])} ({r['venue_source_id'] or '-'}, "
              f"{'우선 출처' if r['venue_source_id'] in db.all_preferred_ids(con) else '우선 출처 밖'}) · Stage {''.join(paper_stages(con, r['paper_id'])) or '-'}"
              f"<br>경로: {md_cell(' | '.join(discovery_paths(con, r['paper_id'])))} |",
              (lambda rk: f"| Research Importance | " + (
                  f"Core 순위 **{rk['rank_label'] or '-'}** · 역할별 {role_rank_text(rk)} · 점수 {rk['importance_score'] if rk['importance_score'] is not None else '평가보류'}"
                  f" · 등급 {TIER_KO.get(rk['importance_tier'], rk['importance_tier'] or '미지정')} · {rk['proposed_or_approved']} ({rk['version_id']}·{rk['weight_version']}, {rk['basis']} 기반)"
                  + (f"<br>불확실성: {md_cell(rk['uncertainty'])}" if rk['uncertainty'] else "") + " — 상세: 03a 부록 F"
                  if rk else "미평가") + " |")(ranking(con, r["paper_id"])),
              f"| Provenance | 원문 {fts}" + (lambda v: f"({VERSION_KO.get(v, v)}{' — 수치·쪽수는 출판본과 대조 필요' if v and v != 'publishedVersion' else ''})" if v else "")(
                  (con.execute("SELECT version FROM fulltexts WHERE paper_id=? AND access_status='FULL_PDF'", (r['paper_id'],)).fetchone() or [None])[0])
              + f" · 분석 기반 **{basis}** · 서지검증 {r['bib_match'] or 'NOT_CHECKED'} · 출판상태 {db.integrity_status(con, r['paper_id'])}"
              + (f" · 판본: {md_cell(r['version_note'])}" if r['version_note'] else "") + " |", ""]
        if not card:
            L += ["_분석카드 미작성_", ""]
            continue
        for k, lab in CARD_FIELDS:
            v = card.get(k)
            if k == "quality_limitations" and q:
                v = {"연구유형": q["study_type"], "강점": q["strengths"], "한계·위험": q["limitations"],
                     "위험 태그": q["risk_tags"], "검토 상태": q["appraisal_status"], **({"추가 의견": v} if v else {})}
            if k == "citation_candidates":
                evs = con.execute("SELECT * FROM evidence WHERE paper_id=? ORDER BY evidence_id", (r["paper_id"],)).fetchall()
                if evs:
                    v = [f"{e['evidence_id']} [{e['verification_status']}] "
                         + (f"\"{e['verbatim_short_quote']}\" (PDF p.{e['pdf_page']}) — " if e['verbatim_short_quote'] and e['verification_status'] in ('VERIFIED_FULLTEXT', 'ABSTRACT_BASED') else "")
                         + (e["paraphrase"] or "") for e in evs]
            L += [f"**{lab}**", "", _fmt(v), ""]
        L.append("---\n")
    return "\n".join(L) + "\n"


VERSION_KO = {"publishedVersion": "출판본", "acceptedVersion": "게재승인 원고", "submittedVersion": "투고본·프리프린트"}


def r08(con, paths):
    L = ["# 08 인용근거 라이브러리", "",
         "> 검증상태는 스크립트가 원문 텍스트와 글자 대조해 부여한다. `VERIFIED_FULLTEXT`만 직접 인용·쪽수를 그대로 써도 된다.",
         "> `PDF p.`는 PDF 파일의 쪽 순번이며, 학술지 인쇄 쪽수(printed)와 다를 수 있다.", "",
         "| Evidence | 논문 | 유형 | 주장(claim) | 짧은 원문 발췌 | 국문 재서술 | 위치 | 검증상태 | 비고 |", "|---|---|---|---|---|---|---|---|---|"]
    stats = Counter()
    for e in con.execute("SELECT * FROM evidence ORDER BY evidence_id"):
        p = paper(con, e["paper_id"])
        stats[e["verification_status"]] += 1
        quote = e["verbatim_short_quote"] if e["verification_status"] in ("VERIFIED_FULLTEXT", "ABSTRACT_BASED") else (
            "(미검증 — 사용 금지)" if e["verbatim_short_quote"] else "")
        loc = []
        if e["pdf_page"] and e["verification_status"] in ("VERIFIED_FULLTEXT", "CONFLICT", "UNVERIFIED") and db.fulltext_status(con, e["paper_id"]) in ("FULL_PDF", "FULL_XML"):
            ver = (con.execute("SELECT version FROM fulltexts WHERE paper_id=?", (e["paper_id"],)).fetchone() or [None])[0]
            loc.append(f"PDF p.{e['pdf_page']}" + (f" ({VERSION_KO.get(ver, ver)} — 출판본 쪽수 아님)" if ver and ver != "publishedVersion" else ""))
        if e["printed_page"]:
            loc.append(f"인쇄 p.{e['printed_page']}")
        if e["section_or_table"]:
            loc.append(e["section_or_table"])
        L.append(f"| {e['evidence_id']} | {e['paper_id']} {md_cell(short_cite(p))} | {e['type']} | {md_cell(e['claim'])} | "
                 f"{md_cell(quote) if quote else '-'} | {md_cell(e['paraphrase'])} | {md_cell(', '.join(loc) or '-')} | "
                 f"{e['verification_status']} | {md_cell(e['verification_note'])} |")
    L += ["", "검증상태 집계: " + (", ".join(f"{k} {v}" for k, v in stats.items()) or "없음")]
    return "\n".join(L) + "\n"


def r10(con, paths):
    ver, prof = latest_profile(con)
    L = ["# 10 연구계획 문헌활용계획 (Literature Utilization Matrix)", "",
         "> '추천 문헌'과 '그 문헌이 해당 가설을 실제로 뒷받침한다는 판단'은 다르다. 최종 인용 여부는 연구자가 승인한다.", "",
         "| 계획서 버전 | 장·절 | 가설·변수 | 활용 논문 | 중요도(순위·등급) | 근거 ID | 활용 목적 | 인용 방식 | 서술 방향 | 한계·주의 | 근거 검증상태 | 승인 |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for m in con.execute("SELECT * FROM section_mappings ORDER BY section_id, mapping_id"):
        p = paper(con, m["paper_id"])
        evs = con.execute("SELECT verification_status FROM evidence WHERE evidence_id=?", (m["evidence_id"],)).fetchone() if m["evidence_id"] else None
        L.append(f"| {m['plan_version']} | {md_cell(m['section_id'])} {md_cell(m['section_title'])} | {md_cell(m['hypothesis_or_variable'] or '-')} | "
                 f"{m['paper_id']} {md_cell(short_cite(p) if p else '⚠없는 ID')} | "
                 f"{(lambda rk: (rk['rank_label'] or '보류') + ' · ' + (rk['importance_tier'] or '-') + ('' if db.fulltext_status(con, m['paper_id']) in ('FULL_PDF', 'FULL_XML') else ' · 원문 요청 우선') if rk else '미평가')(ranking(con, m['paper_id']))} | "
                 f"{m['evidence_id'] or '-'} | {md_cell(m['use_purpose'])} | "
                 f"{md_cell(m['citation_mode'])} | {md_cell(m['drafting_guidance'])} | {md_cell(m['limitation'] or '-')} | "
                 f"{evs[0] if evs else '근거 미연결'} | {m['approval']} |")
    n = notes(paths, "10")
    if n:
        L += ["", "## 장·절별 작성 안내", "", n]
    return "\n".join(L) + "\n"


def r12(con, paths):
    L = ["# 12 연구공백 및 확장계획", "",
         "> 연구공백은 **조사된 문헌 범위 내에서 검토가 필요한 가설**이다. 해당 연구가 세상에 없다는 주장이 아니다.", "",
         "| Gap | 현재 근거 | 부족 영역 | 우선순위 | 계획된 추가 탐색 | 추가 편수 결정 근거 | 상태 |", "|---|---|---|---|---|---|---|"]
    for g in con.execute("SELECT * FROM research_gaps ORDER BY gap_id"):
        L.append(f"| {g['gap_id']} | {md_cell(g['current_evidence'])} | {md_cell(g['missing_scope'])} | {g['priority']} | "
                 f"{md_cell(g['planned_search'])} | {md_cell(g['decision_basis'])} | {g['status']} |")
    n = notes(paths, "12")
    if n:
        L += ["", n]
    return "\n".join(L) + "\n"


def r13(con, paths):
    L = ["# 13 피드백·확장 이력", "", "## 1. 연구계획 버전", "", "| 버전 | 번호 | 원본 | 등록 | 변경 요약 |", "|---|---|---|---|---|"]
    for v in con.execute("SELECT * FROM research_versions ORDER BY version_no"):
        d = json.loads(v["diff_json"] or "{}")
        summ = "; ".join(f"{k}: +{len(x.get('added', []))}/-{len(x.get('removed', []))}" if isinstance(x, dict) else f"{k}: 변경"
                         for k, x in d.items()) or ("최초 버전" if v["version_no"] == 1 else "변경 없음")
        L.append(f"| {v['version_id']} | {v['version_no']} | {md_cell(v['source_file'])} | {v['uploaded_at']} | {md_cell(summ)} |")
    for v in con.execute("SELECT * FROM research_versions WHERE diff_json IS NOT NULL AND diff_json!='{}' ORDER BY version_no"):
        L += ["", f"### {v['version_id']} 변경 상세", "", "| 항목 | 추가 | 삭제 |", "|---|---|---|"]
        for k, x in json.loads(v["diff_json"]).items():
            if isinstance(x, dict):
                L.append(f"| {k} | {md_cell('<br>'.join(map(str, x.get('added', []))) or '-')} | {md_cell('<br>'.join(map(str, x.get('removed', []))) or '-')} |")
            else:
                L.append(f"| {k} | {md_cell(x)} | |")
    L += ["", "## 2. 피드백·질문 대응", "", "| ID | 버전 | 유형 | 요청 | 기존 근거 충족 | 결정 | 승인 | 검색 Run | 추가 문헌 |", "|---|---|---|---|---|---|---|---|---|"]
    for f in con.execute("SELECT * FROM feedback_runs ORDER BY feedback_id"):
        L.append(f"| {f['feedback_id']} | {f['version_id']} | {f['kind']} | {md_cell(f['request'])} | {md_cell(f['sufficiency'])} | "
                 f"{md_cell(f['decision'])} | {f['approval'] or 'PENDING'} | {md_cell(f['resulting_search_runs'] or '-')} | {md_cell(f['added_papers'] or '-')} |")
    L += ["", "## 3. 문헌 구분 변경 이력", "", "| 일시 | P-ID | 구분 | 상태 | 사유 | 버전 |", "|---|---|---|---|---|---|"]
    for h in con.execute("SELECT * FROM membership_history WHERE tier!='CANDIDATE' OR status!='PROPOSED' ORDER BY id"):
        L.append(f"| {h['changed_at']} | {h['paper_id']} | {h['tier']} | {h['status']} | {md_cell(h['reason'])} | {h['version_id']} |")
    L += ["", "## 4. 중요도 순위 변경(버전별)", "",
          "| P-ID | 버전·가중치 | 점수 | 순위 | 상태 | 변경 사유 | 평가일 |", "|---|---|---|---|---|---|---|"]
    for r in con.execute("SELECT * FROM paper_rankings ORDER BY paper_id, eval_id"):
        L.append(f"| {r['paper_id']} | {r['version_id']}·{r['weight_version']} | {r['importance_score'] if r['importance_score'] is not None else '보류'} | "
                 f"{r['rank_label'] or '-'} | {r['proposed_or_approved']}{'' if r['is_current'] else ' (이전)'} | {md_cell(r['change_reason'] or '-')} | {r['evaluated_at']} |")
    n = notes(paths, "13")
    if n:
        L += ["", n]
    return "\n".join(L) + "\n"


def narrative(con, paths, key: str, title: str, appendix: bool):
    body = notes(paths, f"{key}_body")
    if body is None:
        return None
    cited: set = set()
    if "{{RANKING_TABLE}}" in body:
        body = body.replace("{{RANKING_TABLE}}", RANK_NOTE + "\n\n" + "\n".join(ranking_table(con, "CORE")))
    text = cite_replace(con, body, cited)
    L = [text.rstrip(), ""]
    if appendix:
        core = member_rows(con)
        L += ["## Ⅷ. 참고문헌·검증 현황", "",
              "참고문헌 스타일: APA 7판 근사(투고 전 서지관리도구로 재확인). 각 항목 뒤 괄호는 내부 ID·원문·출판상태.", ""]
        for r in sorted(core, key=lambda x: (authors_list(x)[0]["family"] if authors_list(x) else x["title"] or "")):
            L.append(f"- {apa(r)} `[{r['paper_id']} · {db.fulltext_status(con, r['paper_id'])} · "
                     f"서지 {r['bib_match'] or 'NOT_CHECKED'} · {db.integrity_status(con, r['paper_id'])}]`")
        uncited = [r["paper_id"] for r in core if r["paper_id"] not in cited]
        L += ["", f"본문 인용 {len(cited)}편 / Core+Extended {len(core)}편" + (f" — 본문 미인용: {', '.join(uncited)}" if uncited else ""),
              "", "## 부록", "",
              "- 부록 A: 논문별 분석카드 → `07_논문별_분석카드.md`",
              "- 부록 B: 인용근거 라이브러리 → `08_인용근거_라이브러리.md`",
              "- 부록 C: 원문 다운로드 요청목록 → `06_원문다운로드_요청목록.md`",
              "- 부록 D: 검색·선별 로그 → `04_검색_선별_이력.md`",
              "- 부록 E: 분야별 우선 학술출처 선정표 → `01a_우선학술출처_선정표.md`",
              "- 부록 F: 논문 중요도 산정 기준·상세 점수·역할별 순위·변경 이력 → `03a_논문별_중요도_순위표.md`", ""]
    else:
        L += ["## 인용 문헌", ""]
        for pid in sorted(cited):
            L.append(f"- [{pid}] {apa(paper(con, pid))}")
    return "\n".join(L) + "\n"


OUTPUTS = {
    "01": ("01_연구맥락_검색전략.md", r01),
    "01a": ("01a_우선학술출처_선정표.md", r01a),
    "01b": ("01b_국내외_검색모드_통합이력.md", r01b),
    "02": ("02_Seed_3편_선정표.md", r02),
    "03": ("03_핵심문헌_20편.csv", r03),
    "03a": ("03a_논문별_중요도_순위표.md", r03a),
    "04": ("04_검색_선별_이력.md", r04),
    "05": ("05_원문확보_현황.csv", r05),
    "06": ("06_원문다운로드_요청목록.md", r06),
    "07": ("07_논문별_분석카드.md", r07),
    "08": ("08_인용근거_라이브러리.md", r08),
    "09": ("09_선행연구_비교분석.md", lambda c, p: narrative(c, p, "09", "비교분석", False)),
    "10": ("10_연구계획_문헌활용계획.md", r10),
    "11": ("11_선행연구_종합보고서.md", lambda c, p: narrative(c, p, "11", "종합보고서", True)),
    "12": ("12_연구공백_및_확장계획.md", r12),
    "13": ("13_피드백_확장_이력.md", r13),
}


def render(con, paths: Paths, which: list[str] | None = None) -> list[str]:
    done = []
    for key, (fname, fn) in OUTPUTS.items():
        if which and key not in which:
            continue
        if key == "13" and con.execute("SELECT COUNT(*) FROM research_versions").fetchone()[0] < 2 \
                and not con.execute("SELECT COUNT(*) FROM feedback_runs").fetchone()[0] and not which:
            continue  # 2차 산출물: 피드백 이벤트가 생긴 뒤에만
        try:
            text = fn(con, paths)
            if text is not None:
                (paths.out / fname).write_text(text, encoding="utf-8")
                done.append(fname)
            elif fname.endswith(".csv"):
                done.append(fname)
        except PermissionError:
            done.append(f"⚠ {fname} — 파일이 다른 프로그램(Excel 등)에서 열려 있어 갱신하지 못함. 닫고 다시 render")
    return done


def bibtex(con, paths: Paths) -> str:
    out = []
    for r in member_rows(con):
        au = authors_list(r)
        key = f"{re.sub(r'[^A-Za-z]', '', au[0]['family']) if au else 'anon'}{r['year'] or ''}_{r['paper_id'].replace('-', '')}"
        cr = json.loads(r["crossref_json"] or "{}")
        fields = {"title": "{" + (r["title"] or "") + "}",
                  "author": " and ".join(f"{a['family']}, {a['given']}".strip(", ") for a in au),
                  "year": r["year"], "journal": r["venue"], "doi": r["doi"],
                  "volume": cr.get("volume"), "number": cr.get("issue"), "pages": (cr.get("page") or "").replace("-", "--") or None,
                  "note": f"{r['paper_id']}; fulltext={db.fulltext_status(con, r['paper_id'])}; bib={r['bib_match'] or 'NOT_CHECKED'}"}
        typ = "article" if (r["work_type"] or "article") in ("article", "review") else "misc"
        body = ",\n".join(f"  {k} = {{{v}}}" if not str(v).startswith("{") else f"  {k} = {v}" for k, v in fields.items() if v)
        out.append(f"@{typ}{{{key},\n{body}\n}}")
    return "\n\n".join(out) + "\n"
