"""SQLite 상태 저장소(F13). 삭제 대신 상태 변경으로 이력을 보존한다."""
from __future__ import annotations

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY, title TEXT, owner TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS research_versions (
  version_id TEXT PRIMARY KEY, project_id TEXT, version_no INTEGER, source_file TEXT,
  profile_json TEXT, diff_json TEXT, uploaded_at TEXT
);
CREATE TABLE IF NOT EXISTS papers (
  paper_id TEXT PRIMARY KEY,           -- P-001 (불변)
  doi TEXT, openalex_id TEXT, s2_id TEXT,
  title TEXT, year INTEGER, authors_json TEXT, venue TEXT, venue_source_id TEXT, venue_type TEXT,
  work_type TEXT, language TEXT,
  abstract TEXT, abstract_source TEXT,
  cited_by_count INTEGER, referenced_works_json TEXT, related_works_json TEXT,
  oa_json TEXT,                        -- 공개 원문 위치 후보
  is_retracted_openalex INTEGER,
  canonical_work_id TEXT,              -- 같은 연구의 다른 판본이면 대표 P-ID
  version_note TEXT,
  role_tags TEXT,                      -- theory,methodology,empirical (복수 허용)
  crossref_json TEXT, crossref_checked_at TEXT, bib_match TEXT,
  first_seen_run TEXT, created_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_papers_doi ON papers(doi) WHERE doi IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_papers_oa ON papers(openalex_id) WHERE openalex_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS paper_memberships (
  project_id TEXT, paper_id TEXT,
  tier TEXT,               -- CANDIDATE / CORE / EXTENDED
  status TEXT,             -- PROPOSED / APPROVED / REJECTED / REPLACEMENT_NEEDED / RECHECK
  reason TEXT, selected_at TEXT, review_by TEXT, version_id TEXT,
  PRIMARY KEY (project_id, paper_id)
);
CREATE TABLE IF NOT EXISTS membership_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id TEXT, tier TEXT, status TEXT,
  reason TEXT, changed_at TEXT, version_id TEXT
);
CREATE TABLE IF NOT EXISTS seeds (
  role TEXT, paper_id TEXT, reason TEXT, alternatives TEXT, status TEXT,
  provisional INTEGER, set_at TEXT, version_id TEXT, PRIMARY KEY (role, paper_id)
);
CREATE TABLE IF NOT EXISTS search_runs (
  run_id TEXT PRIMARY KEY, db TEXT, run_date TEXT, kind TEXT, purpose TEXT,
  raw_query TEXT, filters TEXT, request_url TEXT, total_count INTEGER, returned INTEGER,
  new_papers INTEGER, parent_paper_id TEXT, version_id TEXT, feedback_id TEXT, note TEXT,
  source_ids TEXT,              -- Stage A에서 적용한 출처 필터(S-ID)
  search_stage TEXT,            -- A(우선 출처) / B(제한 없는 인용망) / C(독립·광역) / U(사용자 추천)
  scope_expansion_reason TEXT,  -- 출처 제한 완화·범위 확대 사유
  api_calls INTEGER, cost_usd REAL
);
CREATE TABLE IF NOT EXISTS preferred_sources (
  source_id TEXT,               -- OpenAlex S-ID (API로 확인한 값만)
  source_name TEXT, source_type TEXT, issn_l TEXT,
  field TEXT,                   -- 세부 분야(예: 과학기술정책)
  role TEXT,                    -- theory / methodology / empirical / general
  scope_fit TEXT,               -- Aims&Scope·수록 연구와 연구질문의 적합성 근거
  source_list_evidence TEXT,    -- is_core, listed_in, DOAJ 등(참고 근거, 품질 보증 아님)
  works_count INTEGER, priority INTEGER, selection_reason TEXT,
  origin TEXT,                  -- agent / user
  status TEXT,                  -- ACTIVE / USER_EXCLUDED / RETIRED
  approval TEXT,                -- PROPOSED / APPROVED / MODIFIED_BY_USER
  version_id TEXT, gap_id TEXT, created_at TEXT, updated_at TEXT,
  PRIMARY KEY (source_id, version_id, gap_id)
);
CREATE TABLE IF NOT EXISTS search_coverage_checks (
  check_id TEXT PRIMARY KEY, run_ids TEXT, stage_counts_json TEXT,
  seed_network_hits INTEGER, independent_hits INTEGER, outside_source_hits INTEGER,
  only_bc_relevant TEXT, important_missed_papers TEXT, decision TEXT, version_id TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS search_hits (
  run_id TEXT, paper_id TEXT, rank INTEGER, PRIMARY KEY (run_id, paper_id)
);
CREATE TABLE IF NOT EXISTS discovery_edges (
  source_paper_id TEXT, target_paper_id TEXT, edge_type TEXT, run_id TEXT,
  PRIMARY KEY (source_paper_id, target_paper_id, edge_type)
);
CREATE TABLE IF NOT EXISTS screening_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id TEXT, stage TEXT, decision TEXT,
  reason TEXT, decided_at TEXT, version_id TEXT
);
CREATE TABLE IF NOT EXISTS fulltexts (
  paper_id TEXT PRIMARY KEY, file_path TEXT, source_url TEXT, source_type TEXT,
  version TEXT, license TEXT, checksum TEXT, size_bytes INTEGER, pages INTEGER,
  title_match REAL, access_status TEXT, attempts_json TEXT, acquired_at TEXT
);
CREATE TABLE IF NOT EXISTS evidence (
  evidence_id TEXT PRIMARY KEY, paper_id TEXT, type TEXT, claim TEXT,
  verbatim_short_quote TEXT, paraphrase TEXT, pdf_page INTEGER, printed_page TEXT,
  section_or_table TEXT, verification_status TEXT, verification_note TEXT,
  created_at TEXT, version_id TEXT
);
CREATE TABLE IF NOT EXISTS analysis_cards (
  paper_id TEXT PRIMARY KEY, card_json TEXT, basis TEXT, updated_at TEXT, version_id TEXT
);
CREATE TABLE IF NOT EXISTS quality_checks (
  paper_id TEXT PRIMARY KEY, study_type TEXT, strengths TEXT, limitations TEXT,
  risk_tags TEXT, appraisal_status TEXT, checked_at TEXT
);
CREATE TABLE IF NOT EXISTS section_mappings (
  mapping_id TEXT PRIMARY KEY, plan_version TEXT, section_id TEXT, section_title TEXT,
  hypothesis_or_variable TEXT, paper_id TEXT, evidence_id TEXT, use_purpose TEXT,
  citation_mode TEXT, drafting_guidance TEXT, limitation TEXT, approval TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS feedback_runs (
  feedback_id TEXT PRIMARY KEY, version_id TEXT, kind TEXT, request TEXT,
  sufficiency TEXT, decision TEXT, approval TEXT, resulting_search_runs TEXT,
  added_papers TEXT, created_at TEXT, resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS research_gaps (
  gap_id TEXT PRIMARY KEY, current_evidence TEXT, missing_scope TEXT, priority TEXT,
  status TEXT, planned_search TEXT, decision_basis TEXT, version_id TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS publication_updates (
  id INTEGER PRIMARY KEY AUTOINCREMENT, paper_id TEXT, status TEXT, source TEXT,
  detail TEXT, checked_at TEXT, follow_up_required INTEGER
);
CREATE TABLE IF NOT EXISTS paper_external_ids (
  paper_id TEXT, id_type TEXT,           -- DOI / KCI / OpenAlex / UCI
  id_value TEXT, title_language TEXT, match_confidence REAL,
  match_status TEXT,                     -- MATCHED / REVIEW_DUPLICATE / DISTINCT
  related_paper_id TEXT, note TEXT, checked_at TEXT,
  PRIMARY KEY (paper_id, id_type, id_value)
);
CREATE TABLE IF NOT EXISTS ranking_weights (
  weight_version TEXT PRIMARY KEY, weights_json TEXT, note TEXT, set_by TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS paper_rankings (
  eval_id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT, version_id TEXT, paper_id TEXT, set_scope TEXT,   -- CORE / EXTENDED
  research_fit REAL, role_contribution REAL, evidence_quality REAL, distinctiveness REAL, time_relevance REAL,
  weight_version TEXT, importance_score REAL,
  ai_rank INTEGER,            -- 점수 기준 제안 순위
  core_rank INTEGER,          -- 사용자 고정(Pin) 반영 최종 순위(집합 내)
  rank_label TEXT,            -- 동점 표기(예: '3=')
  theory_rank INTEGER, methodology_rank INTEGER, empirical_rank INTEGER,
  importance_tier TEXT,       -- A 핵심 활용 / B 적극 참고 / C 보완 참고 / 평가보류
  rationale_json TEXT, uncertainty TEXT, basis TEXT,   -- basis: FULLTEXT / ABSTRACT
  evaluated_at TEXT, proposed_or_approved TEXT,         -- PROPOSED / APPROVED / OVERRIDDEN / PENDING_EVIDENCE
  pinned_rank INTEGER, override_reason TEXT, change_reason TEXT, evaluated_by TEXT,
  is_current INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, command TEXT, detail TEXT
);
"""

ROLES = ("theory", "methodology", "empirical")
TIERS = ("CANDIDATE", "CORE", "EXTENDED")
REVIEW = ("PROPOSED", "APPROVED", "REJECTED", "REPLACEMENT_NEEDED", "RECHECK")
FULLTEXT = ("FULL_PDF", "FULL_XML", "ABSTRACT_ONLY", "REQUEST_USER", "DOWNLOAD_FAILED",
            "UNAVAILABLE", "NOT_CHECKED")
VERIFICATION = ("VERIFIED_FULLTEXT", "ABSTRACT_BASED", "METADATA_ONLY", "UNVERIFIED", "CONFLICT")
INTEGRITY = ("NO_KNOWN_ALERT", "CORRECTED", "RETRACTED", "EXPRESSION_OF_CONCERN", "UNKNOWN")


# 기존 DB 이전(v1.1 → v1.2 → v1.3): 없는 열만 추가
MIGRATIONS = {
    "projects": [("search_mode", "TEXT"), ("mode_settings", "TEXT")],
    "papers": [("venue_source_id", "TEXT"), ("venue_type", "TEXT"),
               ("kci_id", "TEXT"), ("uci", "TEXT"), ("title_ko", "TEXT"), ("title_en", "TEXT"),
               ("kci_citation_count", "INTEGER"), ("wos_citation_count", "INTEGER"), ("db_origin", "TEXT")],
    "search_runs": [("source_ids", "TEXT"), ("search_stage", "TEXT"), ("scope_expansion_reason", "TEXT"),
                    ("api_calls", "INTEGER"), ("cost_usd", "REAL"), ("search_mode", "TEXT"), ("language", "TEXT")],
    "preferred_sources": [("source_db", "TEXT"), ("kci_index_status", "TEXT")],
}
MODES = ("INTERNATIONAL", "KCI", "INTEGRATED")
CRITERIA = ("research_fit", "role_contribution", "evidence_quality", "distinctiveness", "time_relevance")
CRITERIA_KO = {"research_fit": "연구질문 직접 관련성", "role_contribution": "연구 내 역할·기여도",
               "evidence_quality": "연구설계·근거의 적절성", "distinctiveness": "정보의 차별성·보완성",
               "time_relevance": "연구시점 적합성"}
DEFAULT_WEIGHTS = {"research_fit": 0.35, "role_contribution": 0.25, "evidence_quality": 0.20,
                   "distinctiveness": 0.10, "time_relevance": 0.10}
STAGES = ("A", "B", "C", "U")


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def connect(path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols:
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_papers_kci ON papers(kci_id) WHERE kci_id IS NOT NULL")
    if not con.execute("SELECT 1 FROM ranking_weights").fetchone():
        con.execute("INSERT INTO ranking_weights VALUES ('W1', ?, '초기 제안(파일럿으로 조정)', 'default', ?)",
                    (json.dumps(DEFAULT_WEIGHTS), now()))
        con.commit()
    return con


def search_mode(con) -> str:
    r = con.execute("SELECT search_mode FROM projects LIMIT 1").fetchone()
    return (r[0] if r and r[0] else "INTERNATIONAL")


def current_weights(con) -> tuple[str, dict]:
    r = con.execute("SELECT weight_version, weights_json FROM ranking_weights ORDER BY created_at DESC, weight_version DESC LIMIT 1").fetchone()
    return r[0], json.loads(r[1])


def next_id(con, table: str, col: str, prefix: str, width: int = 3) -> str:
    rows = con.execute(f"SELECT {col} FROM {table} WHERE {col} LIKE ?", (prefix + "%",)).fetchall()
    n = 0
    for r in rows:
        tail = r[0][len(prefix):]
        if tail.isdigit():
            n = max(n, int(tail))
    return f"{prefix}{n + 1:0{width}d}"


def project_id(con) -> str:
    r = con.execute("SELECT project_id FROM projects LIMIT 1").fetchone()
    if not r:
        raise SystemExit("프로젝트가 없습니다. 먼저 `lra.py init --title ...`을 실행하세요.")
    return r[0]


def current_version(con) -> str | None:
    r = con.execute("SELECT version_id FROM research_versions ORDER BY version_no DESC LIMIT 1").fetchone()
    return r[0] if r else None


def log_event(con, command: str, detail) -> None:
    con.execute("INSERT INTO events(at, command, detail) VALUES (?,?,?)",
                (now(), command, json.dumps(detail, ensure_ascii=False)))


def set_membership(con, paper_id: str, tier: str, status: str, reason: str = "",
                   review_by: str = "agent") -> None:
    pid = project_id(con)
    ver = current_version(con)
    con.execute("""INSERT INTO paper_memberships(project_id, paper_id, tier, status, reason, selected_at, review_by, version_id)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(project_id, paper_id) DO UPDATE SET tier=excluded.tier, status=excluded.status,
                   reason=excluded.reason, selected_at=excluded.selected_at, review_by=excluded.review_by,
                   version_id=excluded.version_id""",
                (pid, paper_id, tier, status, reason, now(), review_by, ver))
    con.execute("INSERT INTO membership_history(paper_id, tier, status, reason, changed_at, version_id) VALUES (?,?,?,?,?,?)",
                (paper_id, tier, status, reason, now(), ver))


def membership(con, paper_id: str):
    return con.execute("SELECT * FROM paper_memberships WHERE paper_id=?", (paper_id,)).fetchone()


def fulltext_status(con, paper_id: str) -> str:
    r = con.execute("SELECT access_status FROM fulltexts WHERE paper_id=?", (paper_id,)).fetchone()
    return r[0] if r else "NOT_CHECKED"


def integrity_latest(con, paper_id: str):
    """가장 최근 정정·철회 점검 행(status, checked_at, follow_up_required) 또는 None."""
    return con.execute("""SELECT status, checked_at, follow_up_required FROM publication_updates
                          WHERE paper_id=? ORDER BY id DESC LIMIT 1""", (paper_id,)).fetchone()


def integrity_status(con, paper_id: str) -> str:
    r = con.execute("SELECT status FROM publication_updates WHERE paper_id=? ORDER BY id DESC LIMIT 1",
                    (paper_id,)).fetchone()
    return r[0] if r else "NOT_CHECKED"


def preferred_source_ids(con, gap_id: str | None = None) -> list[str]:
    """현재 연구계획 버전(또는 Gap 전용)의 활성 우선 출처 ID."""
    if gap_id:
        rows = con.execute("""SELECT source_id FROM preferred_sources WHERE status='ACTIVE' AND gap_id=?
                              AND source_id NOT LIKE 'KCI:%' ORDER BY priority""", (gap_id,)).fetchall()
        if rows:
            return [r[0] for r in rows]
    r = con.execute("""SELECT version_id FROM preferred_sources WHERE gap_id='' AND source_id NOT LIKE 'KCI:%'
                       ORDER BY created_at DESC LIMIT 1""").fetchone()
    ver = r[0] if r else None
    rows = con.execute("""SELECT source_id FROM preferred_sources WHERE status='ACTIVE' AND gap_id='' AND version_id=?
                          AND source_id NOT LIKE 'KCI:%' ORDER BY priority""", (ver,)).fetchall()
    return [r[0] for r in rows]


def all_preferred_ids(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT source_id FROM preferred_sources WHERE status='ACTIVE'")}


def user_excluded_ids(con) -> list[str]:
    """사용자가 명시적으로 제외를 요청한 출처(모든 Stage에 부정 필터로 적용)."""
    return sorted({r[0] for r in con.execute("SELECT source_id FROM preferred_sources WHERE status='USER_EXCLUDED'")
                   if not r[0].startswith("KCI:")})


def run_stage(run) -> str:
    """검색 Run의 Stage. v1.1에서 만든 Run(Stage 미기록)은 유형으로 추정."""
    if run["search_stage"]:
        return run["search_stage"]
    if run["kind"] in ("backward", "forward", "related"):
        return "B"
    if run["kind"] == "user":
        return "U"
    return "A" if run["source_ids"] else "C"
