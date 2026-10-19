#!/usr/bin/env python
"""Adaptive Literature Review Agent — 명령행 도구.

프로젝트 루트(raw/, state/, library/, out/가 있는 폴더)에서 실행한다:
    python .claude/skills/adaptive-literature-review/scripts/lra.py <command> ...
명령 목록: python lra.py -h
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from lra import db, papers as P, fulltext as FT, render as R, verify as V  # noqa: E402
from lra.config import Http, Paths, load_config, EVIDENCE_TYPES, QUOTE_MAX_WORDS  # noqa: E402
from lra.sources import Crossref, OpenAlex, SemanticScholar, oa_to_record, norm_doi, search_value  # noqa: E402


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def ctx(args):
    paths = Paths(args.root)
    paths.ensure()
    con = db.connect(paths.db)
    cfg = load_config(paths)
    http = Http(paths, use_cache=not args.no_cache)
    return paths, con, cfg, http


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def pid_ok(con, pid):
    if not con.execute("SELECT 1 FROM papers WHERE paper_id=?", (pid,)).fetchone():
        raise SystemExit(f"{pid}: 존재하지 않는 P-ID")
    return pid


# ---------------- 프로젝트·입력 ----------------
def cmd_init(a):
    paths, con, cfg, _ = ctx(a)
    if con.execute("SELECT 1 FROM projects").fetchone():
        out({"status": "exists", "project": dict(con.execute("SELECT * FROM projects").fetchone())})
        return
    con.execute("INSERT INTO projects(project_id, title, owner, created_at, search_mode) VALUES (?,?,?,?,'INTERNATIONAL')", ("PRJ-001", a.title, a.owner, db.now()))
    db.log_event(con, "init", {"title": a.title})
    con.commit()
    (paths.state / "notes").mkdir(exist_ok=True)
    out({"status": "created", "project_id": "PRJ-001", "root": str(paths.root),
         "openalex_api_key": bool(cfg.get("openalex_api_key"))})


def cmd_register_raw(a):
    paths, con, *_ = ctx(a)
    man = V.raw_manifest(paths)
    old = json.loads(paths.raw_manifest.read_text(encoding="utf-8")) if paths.raw_manifest.exists() else {}
    changed = [k for k in old if k in man and old[k] != man[k]]
    if changed:
        raise SystemExit(f"raw/ 원본이 변경됨(불변 원칙 위반): {changed}. 새 버전은 새 파일명으로 추가하세요.")
    merged = {**old, **man}
    paths.raw_manifest.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
    report = []
    for rel in man:
        f = paths.root / rel
        tdir = paths.text / "raw" / f.stem
        suf = f.suffix.lower()
        try:
            if suf == ".pdf":
                n = FT.extract_pages(f, tdir)
                chars = sum(len(x.read_text(encoding="utf-8")) for x in tdir.glob("*.txt"))
                st = "정상" if chars > 200 * max(n, 1) * 0.2 else "처리 제한: 텍스트가 거의 없음(스캔 PDF 가능)"
                report.append({"file": rel, "type": "pdf", "pages": n, "status": st, "text": str(tdir)})
            elif suf == ".docx":
                tdir.mkdir(parents=True, exist_ok=True)
                (tdir / "p001.txt").write_text(FT.extract_docx(f), encoding="utf-8")
                report.append({"file": rel, "type": "docx", "status": "정상(표·각주 일부 손실 가능)", "text": str(tdir)})
            elif suf in (".md", ".txt", ".bib", ".ris", ".csv"):
                report.append({"file": rel, "type": suf[1:], "status": "정상(직접 읽기)"})
            else:
                report.append({"file": rel, "type": suf, "status": "처리 제한: 미지원 형식 — PDF/DOCX/MD로 변환 요청"})
        except Exception as e:
            report.append({"file": rel, "status": f"처리 제한: {e}"})
    db.log_event(con, "register-raw", report)
    con.commit()
    out(report)


PROFILE_LIST_KEYS = ["research_questions", "hypotheses", "theories", "key_concepts", "variables", "methods",
                     "plan_sections", "undetermined"]


def _canon(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True) if isinstance(x, (dict, list)) else str(x)


def profile_diff(old: dict, new: dict) -> dict:
    d = {}
    for k in sorted(set(old) | set(new)):
        o, n = old.get(k), new.get(k)
        if k in PROFILE_LIST_KEYS or isinstance(o, list) or isinstance(n, list):
            os_, ns = {_canon(x) for x in (o or [])}, {_canon(x) for x in (n or [])}
            if os_ != ns:
                d[k] = {"added": sorted(ns - os_), "removed": sorted(os_ - ns), "kept": len(os_ & ns)}
        elif _canon(o) != _canon(n):
            d[k] = {"added": [_canon(n)] if n else [], "removed": [_canon(o)] if o else []}
    return d


def cmd_profile(a):
    paths, con, *_ = ctx(a)
    pid = db.project_id(con)
    if a.action == "show":
        r = con.execute("SELECT * FROM research_versions ORDER BY version_no DESC LIMIT 1").fetchone()
        out({"version": r["version_id"], "profile": json.loads(r["profile_json"])} if r else {"version": None})
        return
    prof = load_json(a.file)
    required = ["topic", "research_questions", "search_strategy"]
    missing = [k for k in required if not prof.get(k)]
    if missing:
        raise SystemExit(f"Research Profile 필수 필드 누락: {missing} (모르면 '미정'으로 채우고 undetermined에 기록)")
    last = con.execute("SELECT * FROM research_versions ORDER BY version_no DESC LIMIT 1").fetchone()
    no = (last["version_no"] + 1) if last else 1
    diff = profile_diff(json.loads(last["profile_json"]), prof) if last else {}
    vid = f"v{no}"
    con.execute("INSERT INTO research_versions VALUES (?,?,?,?,?,?,?)",
                (vid, pid, no, a.source, json.dumps(prof, ensure_ascii=False), json.dumps(diff, ensure_ascii=False), db.now()))
    paths.profile.write_text(json.dumps({"version": vid, **prof}, ensure_ascii=False, indent=1), encoding="utf-8")
    db.log_event(con, "profile-set", {"version": vid, "diff_keys": list(diff)})
    con.commit()
    out({"version": vid, "diff": diff})


# ---------------- 검색·확장 ----------------
def _store_results(con, results, run_id, edge_from=None, edge_type=None):
    new = 0
    pids = []
    for rank, w in enumerate(results, 1):
        rec = oa_to_record(w)
        if not rec.get("title"):
            continue
        pid, is_new = P.upsert(con, rec, run_id)
        new += is_new
        pids.append(pid)
        con.execute("INSERT OR IGNORE INTO search_hits VALUES (?,?,?)", (run_id, pid, rank))
        if edge_from and pid != edge_from:
            con.execute("INSERT OR IGNORE INTO discovery_edges VALUES (?,?,?,?)", (edge_from, pid, edge_type, run_id))
    return pids, new


def _new_run(con, **kw):
    rid = db.next_id(con, "search_runs", "run_id", "R-")
    cols = ["run_id", "db", "run_date", "kind", "purpose", "raw_query", "filters", "request_url", "total_count",
            "returned", "new_papers", "parent_paper_id", "version_id", "feedback_id", "note",
            "source_ids", "search_stage", "scope_expansion_reason", "api_calls", "cost_usd"]
    vals = {"run_id": rid, "db": "OpenAlex", "run_date": db.now(), "version_id": db.current_version(con), **kw}
    con.execute(f"INSERT INTO search_runs({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                [vals.get(c) for c in cols])
    return rid


def _brief(con, pids, limit=None):
    pref = db.all_preferred_ids(con)
    rows = []
    for pid in pids[:limit] if limit else pids:
        p = con.execute("SELECT * FROM papers WHERE paper_id=?", (pid,)).fetchone()
        mark = "★" if p["venue_source_id"] in pref else ""
        rows.append(f"{pid} | {p['year']} | {R.short_cite(p)} | {(p['title'] or '')[:100]} | "
                    f"{mark}{(p['venue'] or '?')[:40]} | cited {p['cited_by_count']}")
    return rows


_SOURCE_FIELD_TOKENS = ("source", "issn", "host_organization", "publisher", "repository", "journal")


def source_restricting_clauses(flt: str | None) -> list[str]:
    """OpenAlex filter 중 출처를 '포함 조건'으로 좁히는 절. 부정(!)만으로 된 제외 조건과 *.type은 허용(#7)."""
    out = []
    for clause in (c.strip() for c in (flt or "").split(",") if c.strip()):
        key, _, val = clause.partition(":")
        key = key.strip().lower()
        if key.endswith(".type") or not any(t in key for t in _SOURCE_FIELD_TOKENS):
            continue
        if not all(v.strip().startswith("!") for v in val.split("|")):
            out.append(clause)
    return out


def _exclusion_filter(con, extra: str = "") -> str:
    """사용자가 제외 요청한 출처는 모든 Stage에서 부정 필터로 뺀다."""
    parts = [x for x in [extra] if x]
    parts += [f"primary_location.source.id:!{sid}" for sid in db.user_excluded_ids(con)]
    return ",".join(parts)


def _gap_of(purpose: str | None) -> str | None:
    return purpose.split(":", 1)[1] if purpose and purpose.startswith("gap:") else None


def cmd_search(a):
    paths, con, cfg, http = ctx(a)
    dbname = a.db or ("kci" if db.search_mode(con) == "KCI" else "openalex")
    if dbname == "kci":
        return _kci_search(a, paths, con, cfg, http)
    oa = OpenAlex(http, cfg, getattr(a, 'all_types', False))
    source_ids = None
    if a.sources:
        source_ids = (db.preferred_source_ids(con, _gap_of(a.purpose)) if a.sources == "preferred"
                      else [x.strip() for x in a.sources.split(",") if x.strip()])
        if not source_ids:
            raise SystemExit("우선 학술출처가 없습니다. `source find`·`source add`로 먼저 등록하세요.")
        unknown = [s for s in source_ids if s not in db.all_preferred_ids(con)]
        if unknown:
            raise SystemExit(f"등록·확인되지 않은 출처 ID: {unknown} — `source add`로 API 확인 후 사용")
    stage = a.stage or ("A" if source_ids else "C")
    if stage == "A" and not source_ids:
        raise SystemExit("Stage A는 우선 출처 필터(--sources)가 필요합니다. 제한 없는 검색은 --stage C.")
    if stage == "C" and (source_ids or source_restricting_clauses(a.filter)):
        raise SystemExit("Stage C(독립·광역 검증)에는 출처 필터를 걸 수 없습니다"
                         + (f": {source_restricting_clauses(a.filter)}" if a.filter else "."))
    if stage == "B":
        raise SystemExit("Stage B는 `expand`(인용망·의미 확장)로 수행합니다.")
    flt = _exclusion_filter(con, a.filter or "")
    body = oa.search(a.query, field=a.field, filters=flt, per_page=a.limit, sort=a.sort, source_ids=source_ids)
    results = (body or {}).get("results", [])
    total = ((body or {}).get("meta") or {}).get("count")
    rid = _new_run(con, kind="search", purpose=a.purpose, raw_query=a.query,
                   filters=f"field={a.field}; {flt}; sort={a.sort or 'relevance'}",
                   total_count=total, returned=len(results), feedback_id=a.feedback,
                   source_ids=",".join(source_ids) if source_ids else None, search_stage=stage,
                   scope_expansion_reason=a.expansion_reason, api_calls=oa.calls, cost_usd=round(oa.cost, 4),
                   search_mode=db.search_mode(con), language="ko" if re.search(r"[\uac00-\ud7a3]", a.query) else "en")
    pids, new = _store_results(con, results, rid)
    con.execute("UPDATE search_runs SET new_papers=? WHERE run_id=?", (new, rid))
    db.log_event(con, "search", {"run": rid, "query": a.query, "stage": stage})
    con.commit()
    out({"run_id": rid, "stage": stage, "source_filter": source_ids, "total_count": total, "returned": len(results),
         "new_papers": new, "api_calls": oa.calls, "cost_usd": round(oa.cost, 4),
         "openalex_budget_remaining_usd": getattr(oa, "remaining_usd", None),
         "papers(★=우선 출처)": _brief(con, pids)})


def cmd_add_paper(a):
    paths, con, cfg, http = ctx(a)
    oa = OpenAlex(http, cfg, getattr(a, 'all_types', False))
    ident = norm_doi(a.doi) if a.doi else a.openalex
    w = oa.work(ident)
    rid = _new_run(con, kind="user", purpose="user_recommended", raw_query=ident, returned=1 if w else 0,
                   note=a.note, feedback_id=a.feedback, search_stage="U", api_calls=oa.calls, cost_usd=oa.cost)
    if not w:
        con.commit()
        raise SystemExit(f"OpenAlex에서 찾지 못함: {ident}. 서지정보를 확인하세요(허구 DOI 등록 금지).")
    pids, new = _store_results(con, [w], rid)
    con.execute("UPDATE search_runs SET new_papers=? WHERE run_id=?", (new, rid))
    con.commit()
    out({"run_id": rid, "paper": _brief(con, pids), "new": bool(new)})


def cmd_expand(a):
    """Stage B: 학술출처 제한 없이 인용망·의미 유사 확장."""
    paths, con, cfg, http = ctx(a)
    if source_restricting_clauses(a.filter):
        raise SystemExit(f"Stage B 확장에는 출처 제한 필터를 걸 수 없습니다: {source_restricting_clauses(a.filter)}")
    oa = OpenAlex(http, cfg, getattr(a, 'all_types', False))
    p = con.execute("SELECT * FROM papers WHERE paper_id=?", (pid_ok(con, a.paper),)).fetchone()
    if not p["openalex_id"]:
        raise SystemExit("OpenAlex ID가 없는 문헌은 인용망 확장 불가")
    excl = _exclusion_filter(con, a.filter or "")
    if a.direction == "backward":
        refs = json.loads(p["referenced_works_json"] or "[]")
        if not refs:
            w = oa.work(p["openalex_id"]) or {}
            refs = [x.split("/")[-1] for x in w.get("referenced_works") or []]
        flt = ",".join(x for x in (f"title_and_abstract.search:{search_value(a.query)}" if a.query else "", excl) if x)
        results = oa.works_by_ids(refs, flt) if refs else []
        total = len(refs)
        if len(results) > a.limit:
            # 인용수로 자르면 인용이 적은 최신·국내 연구부터 빠진다(규칙 7). 결정적 순서로 자르고 기록한다(#10)
            n = len(results)
            results.sort(key=lambda w: (-(w.get("publication_year") or 0), w.get("id") or ""))
            results = results[:a.limit]
            excl = "; ".join(x for x in (excl, f"절단: 연도 내림차순 상위 {a.limit}/{n}편") if x)
    elif a.direction == "forward":
        body = oa.citing(p["openalex_id"], a.query or "", a.limit, excl)
        results = (body or {}).get("results", [])
        total = ((body or {}).get("meta") or {}).get("count")
    else:
        rel = json.loads(p["related_works_json"] or "[]")
        results = oa.works_by_ids(rel, excl)[:a.limit] if rel else []
        total = len(rel)
    rid = _new_run(con, kind=a.direction, purpose=a.purpose or f"{a.direction} from {a.paper}", raw_query=a.query,
                   filters=excl, total_count=total, returned=len(results), parent_paper_id=a.paper,
                   feedback_id=a.feedback, search_stage="B",
                   scope_expansion_reason=a.expansion_reason or "Stage B: 출처 제한 해제(인용망)",
                   api_calls=oa.calls, cost_usd=round(oa.cost, 4))
    pids, new = _store_results(con, results, rid, edge_from=a.paper, edge_type=a.direction)
    con.execute("UPDATE search_runs SET new_papers=? WHERE run_id=?", (new, rid))
    con.commit()
    out({"run_id": rid, "stage": "B", "direction": a.direction, "total_available": total, "returned": len(results),
         "new_papers": new, "api_calls": oa.calls, "cost_usd": round(oa.cost, 4),
         "openalex_budget_remaining_usd": getattr(oa, "remaining_usd", None),
         "papers(★=우선 출처)": _brief(con, pids)})


# ---------------- 우선 학술출처(F01·F02·F04) ----------------
def _list_evidence(s: dict) -> str:
    li = s.get("listed_in") or []
    parts = [f"is_core={s.get('is_core')}"]
    if s.get("is_in_doaj"):
        parts.append("DOAJ")
    if li:
        parts.append("listed_in=" + ",".join(li[:12]) + (f" 외 {len(li) - 12}" if len(li) > 12 else ""))
    return "; ".join(parts) + " (출처 목록 등재 정보일 뿐 품질 보증 아님)"


def cmd_source(a):
    paths, con, cfg, http = ctx(a)
    oa = OpenAlex(http, cfg)
    if a.action == "find":
        if not a.query:
            raise SystemExit("--query 필요")
        body = oa.search_sources(a.query, a.limit, a.type)
        rows = []
        for s in (body or {}).get("results", []):
            rows.append({"source_id": s["id"].split("/")[-1], "name": s.get("display_name"), "type": s.get("type"),
                         "issn_l": s.get("issn_l"), "works": s.get("works_count"),
                         "publisher": s.get("host_organization_name"), "list_evidence": _list_evidence(s),
                         "2yr_mean_citedness(보조지표)": round(((s.get("summary_stats") or {}).get("2yr_mean_citedness") or 0), 2)})
        out({"query": a.query, "api_calls": oa.calls, "results": rows})
        return
    if a.action == "list":
        rows = [dict(r) for r in con.execute("SELECT * FROM preferred_sources ORDER BY gap_id, version_id, priority")]
        out(rows or "등록된 우선 출처 없음")
        return
    if a.action == "add-kci":
        return _source_add_kci(a, paths, con, cfg, http)
    if not a.ids:
        raise SystemExit("출처 ID(S...)를 지정하세요")
    ver = db.current_version(con)
    gap = a.gap or ""
    res = []
    for sid in a.ids:
        sid = sid.split("/")[-1]
        if a.action == "add":
            ex = con.execute("SELECT 1 FROM preferred_sources WHERE source_id=? AND status='USER_EXCLUDED'", (sid,)).fetchone()
            if ex and not a.user_restored:
                raise SystemExit(f"{sid}는 사용자가 제외한 출처입니다. 사용자가 되살리라고 했을 때만 "
                                 "--user-restored \"사용자 요청 내용\"으로 다시 추가하세요(#7).")
            s = oa.source(sid)   # 단건 조회 무료 — 실재 확인
            if not s:
                raise SystemExit(f"{sid}: OpenAlex에 없는 출처 ID(임의 생성 금지)")
            con.execute("""INSERT OR REPLACE INTO preferred_sources(source_id, source_name, source_type, issn_l, field, role, scope_fit, source_list_evidence, works_count, priority, selection_reason, origin, status, approval, version_id, gap_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (sid, s.get("display_name"), s.get("type"), s.get("issn_l"), a.field, a.role, a.scope_fit,
                         _list_evidence(s), s.get("works_count"), a.priority,
                         (a.reason or "") + (f" / 사용자 제외 해제: {a.user_restored}" if ex else ""), a.origin,
                         "ACTIVE", "PROPOSED" if a.origin == "agent" else "MODIFIED_BY_USER", ver, gap,
                         db.now(), db.now()))
            res.append({"source_id": sid, "name": s.get("display_name"), "type": s.get("type")})
        elif a.action in ("exclude", "retire"):
            st = "USER_EXCLUDED" if a.action == "exclude" else "RETIRED"
            n = con.execute("UPDATE preferred_sources SET status=?, selection_reason=COALESCE(selection_reason,'') || ?, updated_at=? WHERE source_id=?",
                            (st, f" / {st}: {a.reason or ''}", db.now(), sid)).rowcount
            if not n and a.action == "exclude":   # 목록에 없던 출처를 사용자가 제외 요청
                s = oa.source(sid)
                if not s:
                    raise SystemExit(f"{sid}: OpenAlex에 없는 출처 ID")
                con.execute("""INSERT INTO preferred_sources(source_id, source_name, source_type, issn_l, field, role, scope_fit, source_list_evidence, works_count, priority, selection_reason, origin, status, approval, version_id, gap_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (sid, s.get("display_name"), s.get("type"), s.get("issn_l"), a.field, a.role, None,
                             _list_evidence(s), s.get("works_count"), None, f"사용자 제외 요청: {a.reason or ''}", "user",
                             "USER_EXCLUDED", "MODIFIED_BY_USER", ver, gap, db.now(), db.now()))
            res.append({"source_id": sid, "status": st})
        elif a.action == "approve":
            con.execute("UPDATE preferred_sources SET approval='APPROVED', updated_at=? WHERE source_id=? AND status='ACTIVE'",
                        (db.now(), sid))
            res.append({"source_id": sid, "approval": "APPROVED"})
    db.log_event(con, f"source-{a.action}", res)
    con.commit()
    out({"action": a.action, "version": ver, "gap": gap or None, "sources": res,
         "active_preferred": db.preferred_source_ids(con, gap or None)})


def _source_add_kci(a, paths, con, cfg, http):
    """KCI 학술지를 우선 출처로 등록. KCI 검색 결과에 같은 학술지명이 실제로 나와야 등록(임의 생성 금지)."""
    from lra.sources import KCI, KCIError, kci_to_record
    ok, why = _kci_ready(paths, cfg)
    if not ok:
        raise SystemExit(f"KCI 비활성: {why}")
    if not a.name or not a.verify_title:
        raise SystemExit("--name(학술지명)과 --verify-title(확인용 제목 검색어)이 필요합니다")
    try:
        _, recs, _ = KCI(http, cfg).article_search(title=a.verify_title, journal=a.name, display=20)
    except KCIError as e:
        raise SystemExit(str(e))
    names = {kci_to_record(r).get("venue") for r in recs}
    if a.name not in names:
        raise SystemExit(f"KCI 검색 결과에서 학술지 '{a.name}'를 확인하지 못함(반환된 학술지: {sorted(n for n in names if n)[:8]})")
    pub = next((kci_to_record(r).get("publisher") for r in recs if kci_to_record(r).get("venue") == a.name), None)
    sid = f"KCI:{a.name}"
    ver = db.current_version(con)
    con.execute("""INSERT OR REPLACE INTO preferred_sources(source_id, source_name, source_type, issn_l, field, role, scope_fit,
                   source_list_evidence, works_count, priority, selection_reason, origin, status, approval, version_id, gap_id,
                   created_at, updated_at, source_db, kci_index_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, a.name, "journal", None, a.field, a.role, a.scope_fit,
                 f"KCI 검색에서 학술지 확인({db.now()[:10]}), 발행기관: {pub or '확인 필요'}", None, a.priority, a.reason,
                 a.origin, "ACTIVE", "PROPOSED" if a.origin == "agent" else "MODIFIED_BY_USER", ver, a.gap or "",
                 db.now(), db.now(), "KCI", a.kci_index_status or "확인 필요(KCI 포털에서 등재 구분 확인)"))
    con.commit()
    out({"source_id": sid, "publisher": pub, "kci_index_status": a.kci_index_status or "확인 필요"})


def _paper_stages(con, pid) -> dict:
    """문헌이 발견된 Stage 집합과 최초 발견 Stage."""
    rows = con.execute("""SELECT s.* FROM search_hits h JOIN search_runs s USING(run_id)
                          WHERE h.paper_id=? ORDER BY s.run_id""", (pid,)).fetchall()
    stages = [db.run_stage(r) for r in rows]
    return {"all": sorted(set(stages)), "first": stages[0] if stages else None}


def cmd_coverage(a):
    """Stage A/B/C 결과 비교(F04·V05): 우선 출처 밖 발견, 독립검색 고유 발견, 호출량."""
    paths, con, *_ = ctx(a)
    pref = db.all_preferred_ids(con)
    runs = con.execute("SELECT * FROM search_runs").fetchall()
    by_stage = {}
    for r in runs:
        st = db.run_stage(r)
        d = by_stage.setdefault(st, {"runs": 0, "returned": 0, "new": 0, "api_calls": 0, "cost_usd": 0.0})
        d["runs"] += 1
        d["returned"] += r["returned"] or 0
        d["new"] += r["new_papers"] or 0
        d["api_calls"] += r["api_calls"] or 0
        d["cost_usd"] = round(d["cost_usd"] + (r["cost_usd"] or 0), 4)
    core = R.member_rows(con)
    detail, only_bc, outside, b_hits, c_hits = [], [], [], 0, 0
    for r in core:
        st = _paper_stages(con, r["paper_id"])
        in_pref = r["venue_source_id"] in pref
        if "B" in st["all"]:
            b_hits += 1
        if "C" in st["all"]:
            c_hits += 1
        if not in_pref:
            outside.append(r["paper_id"])
        if "A" not in st["all"] and st["all"]:
            only_bc.append(r["paper_id"])
        detail.append({"paper_id": r["paper_id"], "stages": "".join(st["all"]), "first": st["first"],
                       "preferred_source": in_pref, "venue": r["venue"]})
    c_only = [d["paper_id"] for d in detail if d["stages"] == "C"]
    n = len(core) or 1
    summary = {"stage_runs": by_stage, "core_extended": len(core),
               "found_via_B": b_hits, "found_via_C": c_hits,
               "outside_preferred_sources": outside,
               "not_found_in_stage_A": only_bc,
               "found_only_by_independent_C": c_only,
               "outside_valid_discovery_ratio": round(len(only_bc) / n, 2),
               "stage_C_runs": by_stage.get("C", {}).get("runs", 0)}
    if a.record:
        cid = db.next_id(con, "search_coverage_checks", "check_id", "CC-")
        con.execute("INSERT INTO search_coverage_checks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, ",".join(r["run_id"] for r in runs), json.dumps(by_stage, ensure_ascii=False),
                     b_hits, c_hits, len(outside), ",".join(only_bc), a.missed or "", a.decision or "",
                     db.current_version(con), db.now()))
        con.commit()
        summary["check_id"] = cid
    out({**summary, "papers": detail})


def cmd_candidates(a):
    paths, con, *_ = ctx(a)
    tiers = a.tier.split(",")
    rows = con.execute(f"""SELECT p.*, m.tier, m.status FROM papers p JOIN paper_memberships m USING(paper_id)
                           WHERE m.tier IN ({','.join('?' * len(tiers))}) AND m.status!='REJECTED'""", tiers).fetchall()
    scored = []
    pref = db.all_preferred_ids(con)
    for r in rows:
        if a.min_year and (r["year"] or 0) < a.min_year:
            continue
        paths_ = R.discovery_paths(con, r["paper_id"])
        n_edges = con.execute("SELECT COUNT(DISTINCT source_paper_id) FROM discovery_edges WHERE target_paper_id=?", (r["paper_id"],)).fetchone()[0]
        n_hits = con.execute("SELECT COUNT(*) FROM search_hits WHERE paper_id=?", (r["paper_id"],)).fetchone()[0]
        dec = con.execute("SELECT decision FROM screening_decisions WHERE paper_id=? ORDER BY id DESC LIMIT 1", (r["paper_id"],)).fetchone()
        if a.undecided and dec:
            continue
        scored.append((n_edges * 2 + n_hits, r, paths_, dec[0] if dec else ""))
    scored.sort(key=lambda x: (-x[0], -(x[1]["cited_by_count"] or 0)))
    scored = scored[:a.top]
    lines = []
    for s, r, pp, dec in scored:
        if a.format == "jsonl":
            lines.append(json.dumps({"paper_id": r["paper_id"], "tier": r["tier"], "score": s, "decision": dec,
                                     "title": r["title"], "year": r["year"], "venue": r["venue"], "venue_source_id": r["venue_source_id"],
                                     "preferred_source": r["venue_source_id"] in pref, "type": r["work_type"],
                                     "cited_by": r["cited_by_count"], "paths": pp,
                                     "abstract": (r["abstract"] or "")[:a.abstract_chars] or None}, ensure_ascii=False))
        else:
            ab = (r["abstract"] or "초록 없음")[:a.abstract_chars]
            lines.append(f"### {r['paper_id']} [{r['tier']}{'/' + dec if dec else ''}] {r['title']} ({r['year']})\n"
                         f"- {R.short_cite(r)} · {'★우선출처 ' if r['venue_source_id'] in pref else ''}{r['venue']} · {r['work_type']} · 피인용 {r['cited_by_count']} · 연결점수 {s}\n"
                         f"- 경로: {' | '.join(pp)}\n- 초록: {ab}\n")
    text = "\n".join(lines)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        out({"written": a.out, "count": len(lines)})
    else:
        print(text)


def cmd_show(a):
    paths, con, *_ = ctx(a)
    p = con.execute("SELECT * FROM papers WHERE paper_id=?", (pid_ok(con, a.paper),)).fetchone()
    d = dict(p)
    for k in ("referenced_works_json", "related_works_json"):
        d[k] = f"{len(json.loads(d[k] or '[]'))}건"
    d["membership"] = dict(db.membership(con, a.paper) or {})
    d["fulltext"] = dict(con.execute("SELECT * FROM fulltexts WHERE paper_id=?", (a.paper,)).fetchone() or {})
    d["paths"] = R.discovery_paths(con, a.paper)
    d["screening"] = [dict(x) for x in con.execute("SELECT * FROM screening_decisions WHERE paper_id=?", (a.paper,))]
    out(d)


SOURCE_EXCLUSION_RE = re.compile(r"출처\s*밖|우선\s*출처|목록에\s*없|비우선|미수록|주요\s*학술지|권위|"
                                 r"not in preferred|outside (the )?preferred|non-?preferred|(venue|journal) (is )?not (listed|indexed|preferred)",
                                 re.I)


EXCLUSION_CATEGORIES = {"relevance": "관련성", "role": "역할", "quality": "품질", "duplicate": "중복", "other": "기타"}


def _exclusion_reason(a) -> str:
    """제외 사유는 분류(관련성/역할/품질/중복/기타)를 고르게 한다. '우선 출처 밖'은 어떤 표현으로도 사유가 될 수 없다(#7)."""
    if not a.category:
        raise SystemExit("제외에는 --category relevance|role|quality|duplicate|other 중 하나가 필요합니다.")
    if SOURCE_EXCLUSION_RE.search(a.reason):
        raise SystemExit("v1.2 원칙: '우선 출처 밖'은 제외 사유가 될 수 없습니다. 연구질문 관련성·역할·품질로 판단하세요.")
    return f"[{EXCLUSION_CATEGORIES[a.category]}] {a.reason}"


def cmd_screen(a):
    paths, con, *_ = ctx(a)
    reason = _exclusion_reason(a) if a.decision == "exclude" else a.reason
    for pid in a.papers:
        pid_ok(con, pid)
        con.execute("INSERT INTO screening_decisions(paper_id, stage, decision, reason, decided_at, version_id) VALUES (?,?,?,?,?,?)",
                    (pid, a.stage, a.decision, reason, db.now(), db.current_version(con)))
    con.commit()
    out({"screened": a.papers, "decision": a.decision})


# ---------------- 선정·승인 ----------------
def _add_roles(con, pid, roles):
    cur = con.execute("SELECT role_tags FROM papers WHERE paper_id=?", (pid,)).fetchone()[0] or ""
    tags = [t for t in cur.split(",") if t]
    for r in roles:
        if r not in db.ROLES:
            raise SystemExit(f"역할은 {db.ROLES} 중 하나")
        if r not in tags:
            tags.append(r)
    con.execute("UPDATE papers SET role_tags=? WHERE paper_id=?", (",".join(tags), pid))


def cmd_seed(a):
    paths, con, *_ = ctx(a)
    pid_ok(con, a.paper)
    if a.action == "set":
        origin = con.execute("SELECT db_origin FROM papers WHERE paper_id=?", (a.paper,)).fetchone()[0]
        if db.search_mode(con) == "KCI" and (origin or "OPENALEX") == "OPENALEX" and not a.user_consented_mix:
            raise SystemExit(f"{a.paper}는 해외 DB 문헌입니다. KCI 모드에서는 사용자 동의 없이 Seed(Core)로 두지 않습니다 "
                             "(--user-consented-mix \"사용자 동의 내용\").")
        con.execute("INSERT OR REPLACE INTO seeds VALUES (?,?,?,?,?,?,?,?)",
                    (a.role, a.paper, a.reason, a.alternatives or "", "PROPOSED", 1 if a.provisional else 0,
                     db.now(), db.current_version(con)))
        _add_roles(con, a.paper, [a.role])
        m = db.membership(con, a.paper)
        if not m or m["tier"] == "CANDIDATE":
            db.set_membership(con, a.paper, "CORE", "PROPOSED", f"Seed({a.role}): {a.reason}",
                              consent_note=a.user_consented_mix)
    elif a.action == "reject":
        con.execute("UPDATE seeds SET status='REJECTED', reason=reason || ' / 교체사유: ' || ? WHERE role=? AND paper_id=?",
                    (a.reason, a.role, a.paper))
    con.commit()
    out({"seed": a.action, "role": a.role, "paper": a.paper})


def cmd_select(a):
    paths, con, *_ = ctx(a)
    for pid in a.papers:
        pid_ok(con, pid)
        p = con.execute("SELECT canonical_work_id, db_origin FROM papers WHERE paper_id=?", (pid,)).fetchone()
        if p[0]:
            raise SystemExit(f"{pid}는 {p[0]}의 다른 판본으로 연결됨 — 대표판을 선정하세요.")
        if (db.search_mode(con) == "KCI" and (p[1] or "OPENALEX") == "OPENALEX" and a.tier == "CORE"
                and not a.user_consented_mix):
            raise SystemExit(f"{pid}는 해외 DB 문헌입니다. KCI 모드에서는 사용자 동의 없이 Core에 넣지 않습니다 "
                             "(--user-consented-mix \"동의 내용\" 또는 --tier EXTENDED).")
        dup = db.pending_duplicates(con, pid)
        if dup:
            print(f"⚠ {pid}는 {', '.join(dup)}와 동일 연구일 수 있음(REVIEW_DUPLICATE) — `dedup --review`로 확인", file=sys.stderr)
        _add_roles(con, pid, [r for r in (a.roles or "").split(",") if r])
        src = con.execute("SELECT venue_source_id FROM papers WHERE paper_id=?", (pid,)).fetchone()[0]
        if src not in db.all_preferred_ids(con) and not a.outside_reason:
            print(f"⚠ {pid}는 우선 출처 밖 문헌입니다 — `--outside-reason`으로 이 출처에서 고른 이유를 따로 적으세요", file=sys.stderr)
        db.set_membership(con, pid, a.tier, "PROPOSED", a.reason, outside_reason=a.outside_reason,
                          consent_note=a.user_consented_mix)
        con.execute("INSERT INTO screening_decisions(paper_id, stage, decision, reason, decided_at, version_id) VALUES (?,?,?,?,?,?)",
                    (pid, "selection", "include", a.reason, db.now(), db.current_version(con)))
    _recompute_ranks(con)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM paper_memberships WHERE tier='CORE' AND status!='REJECTED'").fetchone()[0]
    out({"selected": a.papers, "tier": a.tier, "core_count": n,
         "note": "중요도 순위는 `rank score`로 평가해야 산정됩니다"})


def cmd_deselect(a):
    paths, con, *_ = ctx(a)
    if a.status == "REJECTED":
        a.reason = _exclusion_reason(a)
    for pid in a.papers:
        m = db.membership(con, pid_ok(con, pid))
        db.set_membership(con, pid, m["tier"] if m else "CANDIDATE", a.status, a.reason, review_by=a.by)
        con.execute("INSERT INTO screening_decisions(paper_id, stage, decision, reason, decided_at, version_id) VALUES (?,?,?,?,?,?)",
                    (pid, "selection", "exclude" if a.status == "REJECTED" else "hold", a.reason, db.now(), db.current_version(con)))
    _recompute_ranks(con)
    con.commit()
    out({"updated": a.papers, "status": a.status, "note": "삭제하지 않고 상태·이력만 변경(순위 재계산됨)"})


def cmd_approve(a):
    """사용자 승인 기록. 승인자(--by)는 기본값 없이 반드시 밝힌다(#7)."""
    paths, con, *_ = ctx(a)
    if a.by == "user" and not (a.evidence or "").strip():
        raise SystemExit("--by user에는 사용자가 승인한 말을 --evidence \"...\"로 함께 기록하세요.")
    bad = []
    for pid in a.papers:
        m = db.membership(con, pid_ok(con, pid))
        if not m or m["tier"] == "CANDIDATE" or m["status"] == "REJECTED":
            bad.append(f"{pid}({(m['tier'] + '/' + m['status']) if m else '선정 기록 없음'})")
    if bad:
        raise SystemExit(f"승인 대상이 아닙니다(먼저 select/seed set): {', '.join(bad)}")
    for pid in a.papers:
        m = db.membership(con, pid)
        note = f" / 승인({a.by}): {a.note or ''}" + (f" — 근거: {a.evidence}" if a.evidence else "")
        db.set_membership(con, pid, m["tier"], "APPROVED", (m["reason"] or "") + note, review_by=a.by)
        if a.by == "user":   # Seed 임시선정 표시는 사용자 승인으로만 해제
            con.execute("UPDATE seeds SET status='APPROVED', provisional=0 WHERE paper_id=? AND status='PROPOSED'", (pid,))
    db.log_event(con, "approve", {"papers": a.papers, "by": a.by, "evidence": a.evidence})
    con.commit()
    out({"approved": a.papers, "by": a.by})


def cmd_dedup(a):
    paths, con, *_ = ctx(a)
    if a.review:
        out(P.review_duplicates(con) or "검토 대기 중인 국내외 중복 후보 없음")
        return
    if a.distinct:
        x, y = a.distinct
        P.resolve_duplicate(con, pid_ok(con, x), pid_ok(con, y), "DISTINCT", a.note or "별개 연구(사람 확인)")
        con.commit()
        out({"distinct": [x, y]})
        return
    if a.unlink:
        pid = pid_ok(con, a.unlink)
        keep = P.unlink_versions(con, pid, a.note or "사람 확인")
        if not keep:
            raise SystemExit(f"{pid}는 다른 문헌의 판본으로 연결되어 있지 않습니다.")
        h = con.execute("""SELECT tier FROM membership_history WHERE paper_id=? AND status!='REJECTED'
                           ORDER BY id DESC LIMIT 1""", (pid,)).fetchone()
        m = db.membership(con, pid)
        if m and m["status"] == "REJECTED" and h:   # 판본 통합으로 제외됐던 것을 되돌림(승인은 다시 받아야 함)
            db.set_membership(con, pid, h[0], "PROPOSED", f"{keep}와의 판본 연결 해제 — 재검토")
        _recompute_ranks(con)
        con.commit()
        out({"unlinked": pid, "was_canonical": keep, "pair_status": "REVIEW_DUPLICATE"})
        return
    if a.link:
        keep, other = (pid_ok(con, x) for x in a.link)
        if keep == other:
            raise SystemExit("같은 P-ID끼리는 연결할 수 없습니다.")
        k = con.execute("SELECT canonical_work_id FROM papers WHERE paper_id=?", (keep,)).fetchone()[0]
        if k:
            raise SystemExit(f"{keep}는 이미 {k}의 판본입니다 — 대표판({k})으로 연결하세요(순환·연쇄 연결 방지).")
        o = con.execute("SELECT canonical_work_id FROM papers WHERE paper_id=?", (other,)).fetchone()[0]
        if o and o != keep:
            raise SystemExit(f"{other}는 이미 {o}의 판본입니다 — 먼저 `dedup --unlink {other}`.")
        if con.execute("SELECT 1 FROM papers WHERE canonical_work_id=?", (other,)).fetchone():
            raise SystemExit(f"{other}를 대표판으로 둔 판본이 있습니다 — 그 판본들을 먼저 {keep}로 옮기세요.")
        P.resolve_duplicate(con, keep, other, "MATCHED", a.note or "동일 연구(사람 확인)")
        P.link_versions(con, pid_ok(con, keep), pid_ok(con, other), a.note or "동일 연구의 다른 판본")
        m = db.membership(con, other)
        if m and m["tier"] in ("CORE", "EXTENDED"):
            db.set_membership(con, other, m["tier"], "REJECTED", f"{keep}의 다른 판본으로 통합")
        _recompute_ranks(con)
        con.commit()
        out({"linked": other, "canonical": keep})
        return
    out(P.find_version_duplicates(con))


def _targets(con, a):
    if getattr(a, "papers", None):
        return a.papers
    tiers = a.tier.split(",")
    return [r[0] for r in con.execute(f"SELECT paper_id FROM paper_memberships WHERE tier IN ({','.join('?' * len(tiers))}) AND status!='REJECTED' ORDER BY paper_id", tiers)]


def cmd_refresh(a):
    """OpenAlex 단건 조회(무료)로 메타데이터 보강 — 출처 ID 등 v1.2 필드가 비어 있는 레코드용."""
    paths, con, cfg, http = ctx(a)
    oa = OpenAlex(http, cfg)
    done = []
    for pid in _targets(con, a):
        p = con.execute("SELECT openalex_id, venue_source_id FROM papers WHERE paper_id=?", (pid,)).fetchone()
        if not p["openalex_id"] or (p["venue_source_id"] and not a.all):
            continue
        w = oa.work(p["openalex_id"])
        if w:
            rec = oa_to_record(w)
            con.execute("UPDATE papers SET venue=?, venue_source_id=?, venue_type=? WHERE paper_id=?",
                        (rec["venue"], rec["venue_source_id"], rec["venue_type"], pid))
            done.append({"paper_id": pid, "venue": rec["venue"], "source_id": rec["venue_source_id"]})
    con.commit()
    out(done or "보강할 레코드 없음")


def cmd_bibcheck(a):
    paths, con, cfg, http = ctx(a)
    cr = Crossref(http, cfg)
    res = [P.crossref_verify(con, cr, pid) for pid in _targets(con, a)]
    con.commit()
    out([r for r in res if r.get("result") != "MATCH"] or "모두 MATCH")


def cmd_integrity(a):
    paths, con, cfg, http = ctx(a)
    cr = Crossref(http, cfg)
    oa = OpenAlex(http, cfg, all_types=True)
    res = [P.integrity_check(con, cr, pid, oa) for pid in _targets(con, a)]
    con.commit()
    out([r for r in res if r["status"] != "NO_KNOWN_ALERT"] or "모두 NO_KNOWN_ALERT (철회 없음의 보증은 아님)")


def cmd_fetch(a):
    paths, con, cfg, http = ctx(a)
    s2 = SemanticScholar(http, cfg) if a.s2 else None
    res = []
    for pid in _targets(con, a):
        r = FT.acquire(con, paths, http.s, pid, s2)
        con.commit()
        res.append({k: v for k, v in r.items() if k != "attempts"} | ({"why": [x.get("result") for x in r.get("attempts", [])]} if r.get("attempts") else {}))
    out(res)


def cmd_ingest(a):
    paths, con, *_ = ctx(a)
    res = []
    files = []
    for f in a.files:
        p = Path(f)
        files += sorted(p.glob("*.pdf")) if p.is_dir() else [p]
    for f in files:
        try:
            res.append(FT.ingest_user_file(con, paths, f, a.paper if len(files) == 1 else None))
            con.commit()
        except SystemExit as e:
            res.append({"file": str(f), "error": str(e)})
    out(res)


# ---------------- 분석 결과 등록 ----------------
def cmd_evidence(a):
    paths, con, *_ = ctx(a)
    if a.action == "reverify":
        changed, redone = [], []
        if a.reextract:   # 대조 기준 텍스트를 PDF에서 다시 만든 뒤 재검증(#4)
            for (pid,) in con.execute("SELECT paper_id FROM fulltexts WHERE access_status='FULL_PDF'").fetchall():
                if FT.reextract(con, paths, pid):
                    redone.append(pid)
        for e in con.execute("SELECT * FROM evidence").fetchall():
            st, note = FT.verify_evidence_item(con, paths, dict(e))
            page = e["pdf_page"]
            if st == "VERIFIED_FULLTEXT" and not page:
                page = FT.verified_page(paths, e["paper_id"], e["verbatim_short_quote"] or "")
            if st != e["verification_status"] or page != e["pdf_page"] or "재검증 필요" in (e["verification_note"] or ""):
                con.execute("UPDATE evidence SET verification_status=?, verification_note=?, pdf_page=? WHERE evidence_id=?",
                            (st, note, page, e["evidence_id"]))
                changed.append({"id": e["evidence_id"], "from": e["verification_status"], "to": st})
        con.commit()
        out({"reextracted": redone, "changed": changed} if a.reextract else (changed or "변동 없음"))
        return
    items = load_json(a.file)
    items = items if isinstance(items, list) else [items]
    res = []
    for it in items:
        pid = pid_ok(con, it["paper_id"])
        q = (it.get("verbatim_short_quote") or "").strip()
        if q and len(q.split()) > QUOTE_MAX_WORDS:
            res.append({"paper_id": pid, "rejected": f"인용 {len(q.split())}단어 > {QUOTE_MAX_WORDS} — 짧은 발췌로 줄이세요"})
            continue
        if it.get("type", "finding") not in EVIDENCE_TYPES:   # 오타 유형은 V08 수치 대조를 조용히 건너뛰게 함(#11)
            res.append({"paper_id": pid, "rejected": f"type '{it.get('type')}' — 허용: {', '.join(EVIDENCE_TYPES)}"})
            continue
        st, note = FT.verify_evidence_item(con, paths, it)
        if st != "VERIFIED_FULLTEXT" and db.fulltext_status(con, pid) not in ("FULL_PDF", "FULL_XML"):
            it["pdf_page"] = None   # 원문 없이 쪽수를 기록하지 않음
        if st == "VERIFIED_FULLTEXT" and not it.get("pdf_page"):
            it["pdf_page"] = FT.verified_page(paths, pid, q)   # 자동 보정한 쪽을 저장(#4)
        if st != "VERIFIED_FULLTEXT":
            it["printed_page"] = None   # 인쇄 쪽수는 원문 대조가 끝난 근거에만(#4)
        if it.get("evidence_id"):
            eid = it["evidence_id"]
        else:
            num = pid.split("-")[1]
            eid = db.next_id(con, "evidence", "evidence_id", f"E-{num}-", 2)
        con.execute("""INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (eid, pid, it.get("type", "finding"), it.get("claim"), q or None, it.get("paraphrase"),
                     it.get("pdf_page"), it.get("printed_page"), it.get("section_or_table"), st, note, db.now(),
                     db.current_version(con)))
        res.append({"evidence_id": eid, "status": st, "note": note})
    con.commit()
    out(res)
    if any("rejected" in r for r in res):
        sys.exit(1)   # 항목별 거부가 있으면 0이 아닌 종료 코드(#11)


def cmd_card(a):
    paths, con, *_ = ctx(a)
    items = load_json(a.file)
    items = items if isinstance(items, list) else [items]
    done = []
    for c in items:
        pid = pid_ok(con, c["paper_id"])
        basis = "FULLTEXT" if db.fulltext_status(con, pid) in ("FULL_PDF", "FULL_XML") else "ABSTRACT"
        q = c.pop("quality", None)
        con.execute("INSERT OR REPLACE INTO analysis_cards VALUES (?,?,?,?,?)",
                    (pid, json.dumps(c, ensure_ascii=False), basis, db.now(), db.current_version(con)))
        if q:
            con.execute("INSERT OR REPLACE INTO quality_checks VALUES (?,?,?,?,?,?,?)",
                        (pid, q.get("study_type"), q.get("strengths"), q.get("limitations"),
                         ",".join(q.get("risk_tags") or []), q.get("appraisal_status", "DRAFT(사람 검토 전)"), db.now()))
        done.append({"paper_id": pid, "basis": basis})
    con.commit()
    out(done)


def cmd_mapping(a):
    paths, con, *_ = ctx(a)
    items = load_json(a.file)
    items = items if isinstance(items, list) else [items]
    ids = []
    for m in items:
        pid_ok(con, m["paper_id"])
        if m.get("evidence_id") and not con.execute("SELECT 1 FROM evidence WHERE evidence_id=?", (m["evidence_id"],)).fetchone():
            raise SystemExit(f"{m['evidence_id']}: 없는 근거 ID")
        mid = m.get("mapping_id") or db.next_id(con, "section_mappings", "mapping_id", "M-")
        con.execute("INSERT OR REPLACE INTO section_mappings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (mid, m.get("plan_version") or db.current_version(con), m.get("section_id"), m.get("section_title"),
                     m.get("hypothesis_or_variable"), m["paper_id"], m.get("evidence_id"), m.get("use_purpose"),
                     m.get("citation_mode", "재서술"), m.get("drafting_guidance"), m.get("limitation"),
                     m.get("approval", "PENDING"), db.now()))
        ids.append(mid)
    con.commit()
    out(ids)


def cmd_gap(a):
    paths, con, *_ = ctx(a)
    items = load_json(a.file)
    items = items if isinstance(items, list) else [items]
    ids = []
    for g in items:
        gid = g.get("gap_id") or db.next_id(con, "research_gaps", "gap_id", "G-")
        con.execute("INSERT OR REPLACE INTO research_gaps VALUES (?,?,?,?,?,?,?,?,?)",
                    (gid, g.get("current_evidence"), g.get("missing_scope"), g.get("priority", "중"),
                     g.get("status", "OPEN"), g.get("planned_search"), g.get("decision_basis"),
                     db.current_version(con), db.now()))
        ids.append(gid)
    con.commit()
    out(ids)


def cmd_feedback(a):
    paths, con, *_ = ctx(a)
    if a.action == "add":
        fid = db.next_id(con, "feedback_runs", "feedback_id", "F-")
        con.execute("INSERT INTO feedback_runs(feedback_id, version_id, kind, request, created_at) VALUES (?,?,?,?,?)",
                    (fid, db.current_version(con), a.kind, a.request, db.now()))
        con.commit()
        out({"feedback_id": fid})
        return
    sets, vals = [], []
    for col, v in (("sufficiency", a.sufficiency), ("decision", a.decision), ("approval", a.approval),
                   ("resulting_search_runs", a.runs), ("added_papers", a.added)):
        if v:
            sets.append(f"{col}=?")
            vals.append(v)
    sets.append("resolved_at=?")
    vals.append(db.now())
    con.execute(f"UPDATE feedback_runs SET {', '.join(sets)} WHERE feedback_id=?", (*vals, a.id))
    con.commit()
    out(dict(con.execute("SELECT * FROM feedback_runs WHERE feedback_id=?", (a.id,)).fetchone()))


# ---------------- 산출·검증 ----------------
# ---------------- 검색 모드(International / KCI / Integrated) ----------------
def _kci_probe(paths) -> dict:
    f = paths.state / "kci_probe.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def _kci_ready(paths, cfg) -> tuple[bool, str]:
    if not cfg.get("kci_api_key"):
        return False, "KCI 인증키 없음(KCI_API_KEY)"
    pr = _kci_probe(paths)
    if not pr.get("verified"):
        return False, "KCI 실측 미통과 — `kci probe` 실행 필요"
    return True, f"KCI 실측 통과({pr.get('probed_at')})"


def cmd_mode(a):
    paths, con, cfg, _ = ctx(a)
    prj = db.project_id(con)
    if a.action == "show":
        ok, why = _kci_ready(paths, cfg)
        out({"search_mode": db.search_mode(con), "mode_settings": json.loads(
            con.execute("SELECT mode_settings FROM projects").fetchone()[0] or "{}"),
             "kci_ready": ok, "kci_status": why})
        return
    if not a.mode:   # 빠뜨리면 INTERNATIONAL로 바뀌던 문제(#13)
        raise SystemExit(f"mode set에는 모드가 필요합니다: {' | '.join(db.MODES)}")
    mode = a.mode.upper()
    if mode not in db.MODES:
        raise SystemExit(f"모드는 {db.MODES}")
    old = db.search_mode(con)
    settings = json.loads(con.execute("SELECT mode_settings FROM projects").fetchone()[0] or "{}")
    hist = settings.get("history", [])
    hist.append({"at": db.now(), "from": old, "to": mode, "reason": a.reason, "version": db.current_version(con)})
    settings.update({"history": hist, "domestic_share": a.domestic_share or settings.get("domestic_share"),
                     "note": "통합 모드의 국내외 비중은 연구목적에 따라 유동적(고정 비율 금지)"})
    con.execute("UPDATE projects SET search_mode=?, mode_settings=? WHERE project_id=?",
                (mode, json.dumps(settings, ensure_ascii=False), prj))
    db.log_event(con, "mode-set", {"from": old, "to": mode, "reason": a.reason})
    con.commit()
    ok, why = _kci_ready(paths, cfg)
    res = {"search_mode": mode, "previous": old}
    if mode != "INTERNATIONAL" and not ok:
        res["warning"] = f"KCI 검색은 아직 비활성: {why}. 그 전까지는 OpenAlex 검색만 실행됩니다."
    out(res)


def cmd_kci(a):
    """KCI Open API 실측. 인증키 발급 후 소량 호출로 실제 응답 필드·제약을 확인해 state/kci_probe.json에 기록."""
    from lra.sources import KCI, KCIError, kci_to_record, _findall, _local
    paths, con, cfg, http = ctx(a)
    if not cfg.get("kci_api_key"):
        raise SystemExit("KCI 인증키가 없습니다. 환경변수 KCI_API_KEY 또는 state/config.json의 kci_api_key에 넣은 뒤 다시 실행하세요.")
    kci = KCI(http, cfg)
    probe = {"probed_at": db.now(), "test_title": a.title, "verified": False, "errors": []}
    try:
        total, recs, root = kci.article_search(title=a.title, display=3, cache=False)
        probe["articleSearch"] = {"total": total, "returned": len(recs),
                                  "tags_seen": sorted({_local(e.tag) for r in recs for e in r.iter()})}
        if recs:
            rec = kci_to_record(recs[0])
            probe["articleSearch"]["fields_filled"] = sorted(k for k, v in rec.items() if v not in (None, [], {}, ""))
            probe["articleSearch"]["sample"] = {k: rec.get(k) for k in ("kci_id", "title", "title_en", "year", "venue", "doi")}
            probe["verified"] = bool(rec.get("kci_id") and rec.get("title"))
    except KCIError as e:
        probe["errors"].append(f"articleSearch: {e}")
    try:  # 명세상 title 필수 — 키워드 단독 검색이 되는지 실측
        total, recs, _ = kci.article_search(keyword=a.title, display=1, cache=False)
        probe["keyword_only_supported"] = bool(recs)
    except KCIError as e:
        probe["keyword_only_supported"] = False
        probe["keyword_only_error"] = str(e)
    try:
        recs, root = kci.reference_search(a.title, display=3)
        probe["referenceSearch"] = {"returned": len(recs), "tags_seen": sorted({_local(e.tag) for r in recs for e in r.iter()}),
                                    "note": "참고문헌 원형(자유형식)일 수 있음 — 개별 논문 매칭 확실성은 별도 기록"}
    except KCIError as e:
        probe["errors"].append(f"referenceSearch: {e}")
    probe["api_calls"] = kci.calls
    (paths.state / "kci_probe.json").write_text(json.dumps(probe, ensure_ascii=False, indent=1), encoding="utf-8")
    db.log_event(con, "kci-probe", {"verified": probe["verified"], "errors": probe["errors"]})
    con.commit()
    out(probe)


def _kci_years(flt: str | None) -> tuple[str | None, str | None]:
    """OpenAlex식 연도 필터 → KCI dateFrom/dateTo(YYYYMM).
    2010-2020, 2010-, -2020, >2009, <2021, 2015를 받는다. KCI가 처리할 수 없는 조건은 조용히 버리지 않고 오류(#10)."""
    if not flt:
        return None, None
    dfrom = dto = None
    for clause in (c.strip() for c in flt.split(",") if c.strip()):
        m = re.fullmatch(r"publication_year:(?:(\d{4})?-(\d{4})?|([<>])(\d{4})|(\d{4}))", clause)
        if not m or not any(m.groups()):
            raise SystemExit(f"KCI 검색에서 해석할 수 없는 필터: {clause!r}. "
                             "KCI에는 publication_year(2010-2020, >2009, <2021, 2015)만 적용됩니다.")
        lo, hi, op, y, one = m.groups()
        if one:
            lo = hi = one
        elif op == ">":
            lo = str(int(y) + 1)
        elif op == "<":
            hi = str(int(y) - 1)
        if lo:
            dfrom = f"{lo}01"
        if hi:
            dto = f"{hi}12"
    return dfrom, dto


def _kci_search(a, paths, con, cfg, http):
    from lra.sources import KCI, KCIError, kci_to_record
    ok, why = _kci_ready(paths, cfg)
    if not ok:
        raise SystemExit(f"KCI 검색 비활성: {why}")
    if db.search_mode(con) == "INTERNATIONAL":
        raise SystemExit("현재 검색 모드가 INTERNATIONAL입니다. `mode set KCI|INTEGRATED --reason ...` 후 사용하세요.")
    probe = _kci_probe(paths)
    if a.kci_field != "title" and not probe.get("keyword_only_supported"):
        raise SystemExit("실측 결과 KCI는 제목(title) 없는 검색을 지원하지 않습니다. --kci-field title 사용")
    kci = KCI(http, cfg)
    journals = [None]
    stage = a.stage or "C"
    if a.sources:
        rows = con.execute("""SELECT source_id, source_name FROM preferred_sources WHERE status='ACTIVE' AND source_db='KCI'
                              AND (gap_id=? OR gap_id='')""", (_gap_of(a.purpose) or "",)).fetchall()
        if a.sources != "preferred":
            want = {x.strip() for x in a.sources.split(",")}
            rows = [r for r in rows if r["source_id"] in want]
        if not rows:
            raise SystemExit("KCI 우선 학술지가 없습니다. `source add-kci --name ...`으로 먼저 등록하세요.")
        journals = [r["source_name"] for r in rows]
        stage = "A"
    dfrom, dto = _kci_years(a.filter)
    results, total_sum = [], 0
    try:
        for jn in journals:
            kw = {a.kci_field: a.query}
            total, recs, _ = kci.article_search(**kw, journal=jn, date_from=dfrom, date_to=dto, display=a.limit)
            total_sum += total or 0
            results += recs
    except KCIError as e:
        raise SystemExit(str(e))
    lang = "ko" if re.search(r"[가-힣]", a.query) else "en"
    rid = _new_run(con, db="KCI", kind="search", purpose=a.purpose, raw_query=a.query,
                   filters=f"kci_field={a.kci_field}; dateFrom={dfrom}; dateTo={dto}",
                   total_count=total_sum, returned=len(results), feedback_id=a.feedback,
                   source_ids=",".join(f"KCI:{j}" for j in journals if j) or None, search_stage=stage,
                   scope_expansion_reason=a.expansion_reason, api_calls=kci.calls, cost_usd=0,
                   search_mode=db.search_mode(con), language=lang)
    new, pids, review = 0, [], []
    excluded = {r[0] for r in con.execute("SELECT source_id FROM preferred_sources WHERE status='USER_EXCLUDED' AND source_id LIKE 'KCI:%'")}
    for rank, r in enumerate(results, 1):
        rec = kci_to_record(r)
        if not rec.get("title") or rec.get("venue_source_id") in excluded:   # 사용자 제외 학술지는 결과에서 뺌
            continue
        pid, is_new = P.upsert(con, rec, rid)
        new += is_new
        pids.append(pid)
        con.execute("INSERT OR IGNORE INTO search_hits VALUES (?,?,?)", (rid, pid, rank))
        if db.pending_duplicates(con, pid):
            review.append(pid)
    con.execute("UPDATE search_runs SET new_papers=? WHERE run_id=?", (new, rid))
    con.commit()
    out({"run_id": rid, "db": "KCI", "stage": stage, "journals": [j for j in journals if j], "total_count": total_sum,
         "returned": len(results), "new_papers": new, "review_duplicate": review, "papers": _brief(con, pids)})


# ---------------- 중요도 순위 ----------------
def cmd_rank(a):
    from lra import ranking as RK
    paths, con, *_ = ctx(a)
    if a.action == "score":
        items = load_json(a.file)
        res = RK.add_scores(con, items if isinstance(items, list) else [items], a.by)
    elif a.action == "pin":
        if a.rank is None:   # 빠뜨리면 고정이 아니라 해제로 동작하던 문제(#13)
            raise SystemExit("rank pin에는 --rank N이 필요합니다(해제는 `rank unpin`).")
        res = [RK.pin(con, pid_ok(con, p), a.rank, a.reason, a.by or "agent") for p in a.papers]
    elif a.action == "unpin":
        res = [RK.pin(con, pid_ok(con, p), None, None, a.by or "agent") for p in a.papers]
    elif a.action == "approve":
        if not a.by:
            raise SystemExit("rank approve에는 승인자 --by가 필요합니다(사용자 승인이면 --by user).")
        res = {"approved": RK.approve(con, a.papers or None, a.by), "by": a.by}
    elif a.action == "weights":
        if a.file:
            wv = RK.set_weights(con, load_json(a.file), a.reason or "", a.by)
            n = RK.rescore_with_current_weights(con)
            res = {"weight_version": wv, "rescored": n}
        else:
            wv, w = db.current_weights(con)
            res = {"weight_version": wv, "weights": w,
                   "history": [dict(r) for r in con.execute("SELECT * FROM ranking_weights ORDER BY created_at")]}
    else:  # show
        res = [dict(r) for r in con.execute("""SELECT paper_id, set_scope, core_rank, rank_label, ai_rank, theory_rank,
                     methodology_rank, empirical_rank, importance_score, importance_tier, proposed_or_approved, pinned_rank,
                     version_id, weight_version FROM paper_rankings WHERE is_current=1
                     ORDER BY set_scope, core_rank IS NULL, core_rank""")]
    if a.action != "show":
        db.log_event(con, f"rank-{a.action}", {"papers": getattr(a, "papers", None), "by": a.by})
        con.commit()
    out(res)


def _recompute_ranks(con):
    from lra import ranking as RK
    for s in ("CORE", "EXTENDED"):
        RK.recompute(con, s)



def cmd_render(a):
    paths, con, *_ = ctx(a)
    out(R.render(con, paths, a.keys or None))


def cmd_export_bib(a):
    paths, con, *_ = ctx(a)
    f = paths.out / "14_참고문헌_내보내기.bib"
    f.write_text(R.bibtex(con, paths), encoding="utf-8")
    out({"written": str(f)})


def cmd_verify(a):
    paths, con, *_ = ctx(a)
    text, summ = V.run(con, paths)
    (paths.out / "16_검증결과.md").write_text(text, encoding="utf-8")
    out({"written": "out/16_검증결과.md", **summ})
    if summ["fail"]:
        sys.exit(1)   # FAIL이 있으면 종료 코드 1 — 실행 오류가 아니라 '고칠 것이 있음'(#11)


def cmd_status(a):
    paths, con, cfg, _ = ctx(a)
    prj = con.execute("SELECT * FROM projects").fetchone()
    tiers = {f"{r[0]}:{r[1]}": r[2] for r in con.execute("SELECT tier, status, COUNT(*) FROM paper_memberships GROUP BY tier, status")}
    ft = {r[0]: r[1] for r in con.execute("""SELECT f.access_status, COUNT(*) FROM fulltexts f JOIN paper_memberships m USING(paper_id)
                                             WHERE m.tier IN ('CORE','EXTENDED') AND m.status!='REJECTED' GROUP BY f.access_status""")}
    out({"project": dict(prj) if prj else None, "version": db.current_version(con),
         "papers_total": con.execute("SELECT COUNT(*) FROM papers").fetchone()[0], "memberships": tiers,
         "seeds": [dict(r) for r in con.execute("SELECT role, paper_id, status FROM seeds")],
         "fulltext": ft, "evidence": {r[0]: r[1] for r in con.execute("SELECT verification_status, COUNT(*) FROM evidence GROUP BY 1")},
         "cards": con.execute("SELECT COUNT(*) FROM analysis_cards").fetchone()[0],
         "mappings": con.execute("SELECT COUNT(*) FROM section_mappings").fetchone()[0],
         "gaps": con.execute("SELECT COUNT(*) FROM research_gaps").fetchone()[0],
         "feedback_open": con.execute("SELECT COUNT(*) FROM feedback_runs WHERE resolved_at IS NULL").fetchone()[0],
         "search_runs": con.execute("SELECT COUNT(*) FROM search_runs").fetchone()[0],
         "search_mode": db.search_mode(con), "kci_ready": _kci_ready(paths, cfg)[1],
         "rankings": {r[0]: r[1] for r in con.execute("SELECT proposed_or_approved, COUNT(*) FROM paper_rankings WHERE is_current=1 GROUP BY 1")},
         "review_duplicates": con.execute("SELECT COUNT(*) FROM duplicate_pairs WHERE status='REVIEW_DUPLICATE'").fetchone()[0],
         "openalex_api_key": bool(cfg.get("openalex_api_key"))})


def main():
    ap = argparse.ArgumentParser(description="Adaptive Literature Review Agent tools")
    ap.add_argument("--root", help="프로젝트 루트(기본: 현재 폴더)")
    ap.add_argument("--no-cache", action="store_true", help="HTTP 캐시 무시")
    from lra import __version__
    ap.add_argument("--version", action="version", version=f"adaptive-literature-review {__version__}")
    ap.add_argument("--all-types", action="store_true", help="심사기록·정오표 등 비논문 레코드도 포함")
    sp = ap.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("init"); s.add_argument("--title", required=True); s.add_argument("--owner", default="")
    s.set_defaults(f=cmd_init)
    sp.add_parser("register-raw", help="raw/ 해시 고정 및 텍스트 추출").set_defaults(f=cmd_register_raw)
    s = sp.add_parser("profile"); s.add_argument("action", choices=["set", "show"]); s.add_argument("--file")
    s.add_argument("--source", help="근거가 된 raw 파일"); s.set_defaults(f=cmd_profile)

    s = sp.add_parser("source", help="분야별 우선 학술출처 찾기·등록·제외·승인")
    s.add_argument("action", choices=["find", "add", "add-kci", "exclude", "retire", "approve", "list"])
    s.add_argument("--name", help="add-kci: KCI 학술지명(정확히)"); s.add_argument("--verify-title", help="add-kci: 확인용 제목 검색어")
    s.add_argument("--kci-index-status", help="add-kci: KCI 등재 구분·확인일(포털 확인값)")
    s.add_argument("ids", nargs="*", help="OpenAlex 출처 ID(S...)")
    s.add_argument("--user-restored", help="add: 사용자가 제외했던 출처를 되살리라고 한 내용")
    s.add_argument("--query"); s.add_argument("--type", choices=["journal", "conference", "repository", "book series"])
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--field", help="세부 분야(예: 과학기술정책)")
    s.add_argument("--role", default="general", choices=["theory", "methodology", "empirical", "general"])
    s.add_argument("--priority", type=int, default=5); s.add_argument("--reason"); s.add_argument("--scope-fit")
    s.add_argument("--origin", default="agent", choices=["agent", "user"]); s.add_argument("--gap", help="Gap 전용 목록")
    s.set_defaults(f=cmd_source)

    s = sp.add_parser("search", help="OpenAlex 검색 (Stage A: --sources, Stage C: 제한 없음)")
    s.add_argument("--query", required=True); s.add_argument("--purpose", required=True,
                                                            help="theory|methodology|empirical|independent|gap:<G-ID>")
    s.add_argument("--sources", help="'preferred'(등록된 우선 출처) 또는 S-ID 쉼표구분 → Stage A")
    s.add_argument("--db", choices=["openalex", "kci"], help="기본: 모드가 KCI면 kci, 아니면 openalex")
    s.add_argument("--kci-field", default="title", choices=["title", "keyword", "abstract"], help="KCI 검색 필드")
    s.add_argument("--stage", choices=["A", "C"], help="기본: --sources 있으면 A, 없으면 C")
    s.add_argument("--expansion-reason", help="출처 제한 완화·범위 확대 사유")
    s.add_argument("--field", default="title_and_abstract", choices=["title_and_abstract", "fulltext", "title", "keyword"])
    s.add_argument("--filter", help="OpenAlex filter 문자열 예: publication_year:>2014,type:article")
    s.add_argument("--sort", help="예: cited_by_count:desc"); s.add_argument("--limit", type=int, default=50)
    s.add_argument("--feedback"); s.set_defaults(f=cmd_search)
    s = sp.add_parser("add-paper", help="사용자 추천 문헌 등록(U3)")
    g = s.add_mutually_exclusive_group(required=True); g.add_argument("--doi"); g.add_argument("--openalex")
    s.add_argument("--note"); s.add_argument("--feedback"); s.set_defaults(f=cmd_add_paper)
    s = sp.add_parser("expand", help="Stage B: 출처 제한 없는 인용망·의미 확장")
    s.add_argument("--paper", required=True); s.add_argument("--direction", required=True, choices=["backward", "forward", "related"])
    s.add_argument("--query", help="관련성 필터(제목·초록)"); s.add_argument("--filter"); s.add_argument("--limit", type=int, default=40)
    s.add_argument("--expansion-reason"); s.add_argument("--purpose"); s.add_argument("--feedback"); s.set_defaults(f=cmd_expand)
    s = sp.add_parser("coverage", help="Stage A/B/C 비교·우선 출처 밖 발견 점검")
    s.add_argument("--record", action="store_true", help="점검 결과를 DB에 기록")
    s.add_argument("--missed", help="중요 누락 문헌(P-ID 쉼표) — 사람/에이전트 판단")
    s.add_argument("--decision", help="확장 계속/종료 등 결정"); s.set_defaults(f=cmd_coverage)
    s = sp.add_parser("candidates", help="후보 목록(검토용)")
    s.add_argument("--tier", default="CANDIDATE"); s.add_argument("--top", type=int, default=60)
    s.add_argument("--min-year", type=int); s.add_argument("--undecided", action="store_true")
    s.add_argument("--abstract-chars", type=int, default=500); s.add_argument("--format", default="md", choices=["md", "jsonl"])
    s.add_argument("--out"); s.set_defaults(f=cmd_candidates)
    s = sp.add_parser("show"); s.add_argument("paper"); s.set_defaults(f=cmd_show)
    s = sp.add_parser("screen", help="선별 결정 기록")
    s.add_argument("papers", nargs="+"); s.add_argument("--decision", required=True, choices=["include", "exclude", "hold"])
    s.add_argument("--reason", required=True); s.add_argument("--stage", default="title_abstract")
    s.add_argument("--category", choices=list(EXCLUSION_CATEGORIES), help="exclude일 때 필수: 제외 사유 분류")
    s.set_defaults(f=cmd_screen)

    s = sp.add_parser("seed"); s.add_argument("action", choices=["set", "reject"])
    s.add_argument("--role", required=True, choices=db.ROLES); s.add_argument("--paper", required=True)
    s.add_argument("--reason", required=True); s.add_argument("--alternatives", help="P-ID 쉼표구분")
    s.add_argument("--provisional", action="store_true", help="사용자 확인 전 임시선정")
    s.add_argument("--user-consented-mix", help="KCI 모드에서 해외 문헌을 Seed로 두는 사용자 동의 내용"); s.set_defaults(f=cmd_seed)
    s = sp.add_parser("select", help="Core/Extended 제안")
    s.add_argument("papers", nargs="+"); s.add_argument("--tier", default="CORE", choices=["CORE", "EXTENDED"])
    s.add_argument("--roles", help="theory,methodology,empirical"); s.add_argument("--reason", required=True)
    s.add_argument("--user-consented-mix", help="KCI 모드에서 해외 문헌을 Core에 넣는 사용자 동의 내용")
    s.add_argument("--outside-reason", help="우선 출처 밖 문헌을 고른 이유(일반 선정 사유와 별도, 03 CSV에 기록)")
    s.set_defaults(f=cmd_select)
    s = sp.add_parser("deselect", help="제외/교체/재검토 표시(삭제 아님)")
    s.add_argument("papers", nargs="+"); s.add_argument("--reason", required=True)
    s.add_argument("--status", default="REJECTED", choices=["REJECTED", "REPLACEMENT_NEEDED", "RECHECK"])
    s.add_argument("--category", choices=list(EXCLUSION_CATEGORIES), help="REJECTED일 때 필수: 제외 사유 분류")
    s.add_argument("--by", default="agent"); s.set_defaults(f=cmd_deselect)
    s = sp.add_parser("approve", help="사용자 승인 기록 — 사용자가 승인한 경우에만")
    s.add_argument("papers", nargs="+"); s.add_argument("--by", required=True, help="user(사용자 승인) 또는 승인한 사람")
    s.add_argument("--evidence", help="사용자가 승인한 말(원문) — --by user일 때 필수"); s.add_argument("--note")
    s.set_defaults(f=cmd_approve)
    s = sp.add_parser("dedup", help="판본 중복 후보 탐지/연결")
    s.add_argument("--link", nargs=2, metavar=("KEEP", "OTHER")); s.add_argument("--note")
    s.add_argument("--unlink", metavar="P-ID", help="판본 연결 되돌리기(잘못 연결한 경우)")
    s.add_argument("--review", action="store_true", help="국내외 중복 후보(REVIEW_DUPLICATE) 목록")
    s.add_argument("--distinct", nargs=2, metavar=("A", "B"), help="별개 연구로 확정")
    s.set_defaults(f=cmd_dedup)
    s = sp.add_parser("mode", help="검색 모드 INTERNATIONAL / KCI / INTEGRATED")
    s.add_argument("action", choices=["set", "show"]); s.add_argument("mode", nargs="?", help="set일 때 필수")
    s.add_argument("--reason"); s.add_argument("--domestic-share", help="통합 모드 국내외 비중 메모(고정 비율 아님)")
    s.set_defaults(f=cmd_mode)
    s = sp.add_parser("kci", help="KCI Open API 실측(인증키 필요)")
    s.add_argument("action", choices=["probe"]); s.add_argument("--title", default="혁신"); s.set_defaults(f=cmd_kci)
    s = sp.add_parser("rank", help="논문별 중요도 점수·순위")
    s.add_argument("action", choices=["score", "pin", "unpin", "approve", "weights", "show"])
    s.add_argument("papers", nargs="*"); s.add_argument("--file"); s.add_argument("--rank", type=int)
    s.add_argument("--reason"); s.add_argument("--by", help="pin/unpin 기본 agent, approve는 필수")
    s.set_defaults(f=cmd_rank)
    for name, fn, hlp in (("bibcheck", cmd_bibcheck, "Crossref 서지 대조"), ("integrity", cmd_integrity, "정정·철회 점검"),
                          ("fetch", cmd_fetch, "공개 원문 다운로드"),
                          ("refresh", cmd_refresh, "출처 ID 등 메타데이터 보강(무료 단건 조회)")):
        s = sp.add_parser(name, help=hlp); s.add_argument("--papers", nargs="*"); s.add_argument("--tier", default="CORE,EXTENDED")
        if name == "fetch":
            s.add_argument("--s2", action="store_true", help="Semantic Scholar 공개 PDF도 시도")
        if name == "refresh":
            s.add_argument("--all", action="store_true", help="이미 값이 있어도 다시 조회")
        s.set_defaults(f=fn)
    s = sp.add_parser("ingest-fulltext", help="사용자 제공 PDF 연결·승격")
    s.add_argument("files", nargs="+"); s.add_argument("--paper"); s.set_defaults(f=cmd_ingest)

    s = sp.add_parser("evidence"); s.add_argument("action", choices=["add", "reverify"]); s.add_argument("--file")
    s.add_argument("--reextract", action="store_true", help="reverify 전에 PDF에서 쪽별 텍스트를 다시 추출")
    s.set_defaults(f=cmd_evidence)
    s = sp.add_parser("card", help="분석카드(+품질) 등록"); s.add_argument("--file", required=True); s.set_defaults(f=cmd_card)
    s = sp.add_parser("mapping", help="장절 활용 매핑 등록"); s.add_argument("--file", required=True); s.set_defaults(f=cmd_mapping)
    s = sp.add_parser("gap", help="연구공백 등록"); s.add_argument("--file", required=True); s.set_defaults(f=cmd_gap)
    s = sp.add_parser("feedback"); s.add_argument("action", choices=["add", "resolve"])
    s.add_argument("--kind", choices=["plan_version", "analysis_question", "review_comment", "new_paper", "other"])
    s.add_argument("--request"); s.add_argument("--id"); s.add_argument("--sufficiency"); s.add_argument("--decision")
    s.add_argument("--approval"); s.add_argument("--runs"); s.add_argument("--added"); s.set_defaults(f=cmd_feedback)

    s = sp.add_parser("render", help="out/ 산출물 생성"); s.add_argument("keys", nargs="*", help="01 02 ... 13")
    s.set_defaults(f=cmd_render)
    sp.add_parser("export-bib").set_defaults(f=cmd_export_bib)
    sp.add_parser("verify", help="out/16_검증결과.md").set_defaults(f=cmd_verify)
    sp.add_parser("status").set_defaults(f=cmd_status)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
