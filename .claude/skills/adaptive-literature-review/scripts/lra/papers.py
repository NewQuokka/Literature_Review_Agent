"""문헌 등록·중복제거(F02·F04), 서지검증(V01), 출판상태 점검(F14)."""
from __future__ import annotations

import difflib
import json
import re
import unicodedata

from . import db
from .config import INTEGRITY_MAX_AGE
from .sources import Crossref, OpenAlex, norm_doi

PREPRINT_TYPES = {"preprint", "posted-content"}


def norm_title(t: str | None) -> str:
    if not t:
        return ""
    t = unicodedata.normalize("NFKC", t).lower()
    t = re.sub(r"<[^>]+>", " ", t)          # 태그 제거
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_sim(a: str | None, b: str | None) -> float:
    a, b = norm_title(a), norm_title(b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def first_surname(authors_json: str | list | None) -> str:
    authors = json.loads(authors_json) if isinstance(authors_json, str) else (authors_json or [])
    if not authors:
        return ""
    name = (authors[0].get("name") or "").strip()
    return norm_title(name.split()[-1]) if name else ""


def _titles(row_or_rec) -> list[str]:
    keys = ("title", "title_ko", "title_en")
    out = []
    for k in keys:
        try:
            v = row_or_rec[k]
        except (KeyError, IndexError):
            v = None
        if v and norm_title(v) not in out:
            out.append(norm_title(v))
    return out


def _same_script_author_ok(a_json, b_json) -> bool | None:
    """첫 저자 성 비교. 한글/로마자처럼 표기 체계가 다르면 판단 불가(None)."""
    a, b = first_surname(a_json), first_surname(b_json)
    if not a or not b:
        return None
    ka = any("\uac00" <= ch <= "\ud7a3" for ch in a)
    kb = any("\uac00" <= ch <= "\ud7a3" for ch in b)
    if ka != kb:
        return None
    return a == b or a in b or b in a


def find_existing(con, rec: dict):
    if rec.get("kci_id"):
        r = con.execute("SELECT * FROM papers WHERE kci_id=?", (rec["kci_id"],)).fetchone()
        if r:
            return r
    if rec.get("doi"):
        r = con.execute("SELECT * FROM papers WHERE doi=?", (rec["doi"],)).fetchone()
        if r:
            return r
    if rec.get("openalex_id"):
        r = con.execute("SELECT * FROM papers WHERE openalex_id=?", (rec["openalex_id"],)).fetchone()
        if r:
            return r
    if rec.get("title") and not rec.get("doi"):
        mine = set(_titles(rec))
        for r in con.execute("SELECT * FROM papers WHERE year BETWEEN ? AND ?",
                             ((rec.get("year") or 0) - 1, (rec.get("year") or 9999) + 1)):
            if mine & set(_titles(r)):
                # 제목이 같아도 첫 저자가 명백히 다르면 별개 연구
                if _same_script_author_ok(json.dumps(rec.get("authors") or []), r["authors_json"]) is False:
                    continue
                return r
    return None


def possible_cross_db_duplicates(con, pid: str, rec: dict, threshold: float = 0.9) -> list[tuple[str, float]]:
    """KCI↔해외 레코드 사이의 불확실한 동일 연구 후보(번역 제목 등). 자동 병합하지 않는다."""
    out = []
    mine = _titles(rec)
    for r in con.execute("SELECT * FROM papers WHERE paper_id!=? AND year BETWEEN ? AND ?",
                         (pid, (rec.get("year") or 0) - 1, (rec.get("year") or 9999) + 1)):
        if (r["db_origin"] or "OPENALEX") == (rec.get("db_origin") or "OPENALEX"):
            continue
        best = max((difflib.SequenceMatcher(None, a, b).ratio() for a in mine for b in _titles(r)), default=0)
        if best >= threshold:
            out.append((r["paper_id"], round(best, 3)))
    return out


def record_external_ids(con, pid: str, rec: dict, status: str = "MATCHED", related: str | None = None,
                        confidence: float | None = None, note: str | None = None):
    for id_type, key in (("DOI", "doi"), ("KCI", "kci_id"), ("OpenAlex", "openalex_id"), ("UCI", "uci")):
        if rec.get(key):
            lang = "ko" if rec.get("title_ko") else "en"
            con.execute("""INSERT OR REPLACE INTO paper_external_ids VALUES (?,?,?,?,?,?,?,?,?)""",
                        (pid, id_type, rec[key], lang, confidence if confidence is not None else 1.0,
                         status, related, note, db.now()))


def upsert(con, rec: dict, run_id: str | None = None) -> tuple[str, bool]:
    """반환: (paper_id, 신규여부). 기존 문헌이면 비어 있는 필드만 보강한다."""
    ex = find_existing(con, rec)
    if ex:
        pid = ex["paper_id"]
        updates = {}
        for col, key in (("doi", "doi"), ("openalex_id", "openalex_id"), ("abstract", "abstract"),
                         ("venue", "venue"), ("venue_source_id", "venue_source_id"), ("venue_type", "venue_type"),
                         ("work_type", "work_type"), ("language", "language"), ("kci_id", "kci_id"),
                         ("uci", "uci"), ("title_ko", "title_ko"), ("title_en", "title_en")):
            if not ex[col] and rec.get(key):
                updates[col] = rec[key]
        if rec.get("abstract") and not ex["abstract"]:
            updates["abstract_source"] = rec.get("abstract_source", "openalex")
        if rec.get("cited_by_count") is not None:
            updates["cited_by_count"] = rec["cited_by_count"]
        if rec.get("referenced_works"):
            updates["referenced_works_json"] = json.dumps(rec["referenced_works"])
        if rec.get("related_works"):
            updates["related_works_json"] = json.dumps(rec["related_works"])
        for k in ("kci_citation_count", "wos_citation_count"):
            if rec.get(k) is not None:
                updates[k] = rec[k]
        # KCI의 상세 페이지 정보로 OpenAlex OA 위치를 덮어쓰지 않는다
        if rec.get("oa") and (rec.get("db_origin") != "KCI" or not ex["oa_json"] or ex["oa_json"] == "{}"):
            updates["oa_json"] = json.dumps(rec["oa"], ensure_ascii=False)
        if (ex["db_origin"] or "OPENALEX") != (rec.get("db_origin") or "OPENALEX") and ex["db_origin"] != "BOTH":
            updates["db_origin"] = "BOTH"
        if "is_retracted" in rec:
            updates["is_retracted_openalex"] = rec["is_retracted"]
        if updates:
            # UNIQUE 충돌(같은 DOI가 다른 행에 있음)은 판본 연결 후보이므로 건너뜀
            try:
                con.execute(f"UPDATE papers SET {', '.join(k + '=?' for k in updates)} WHERE paper_id=?",
                            (*updates.values(), pid))
            except Exception:
                pass
        record_external_ids(con, pid, rec, "MATCHED")
        return pid, False
    pid = db.next_id(con, "papers", "paper_id", "P-")
    con.execute("""INSERT INTO papers(paper_id, doi, openalex_id, title, year, authors_json, venue, venue_source_id,
                   venue_type, work_type, language, abstract, abstract_source, cited_by_count, referenced_works_json,
                   related_works_json, oa_json, is_retracted_openalex, role_tags, first_seen_run, created_at,
                   kci_id, uci, title_ko, title_en, kci_citation_count, wos_citation_count, db_origin)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, rec.get("doi"), rec.get("openalex_id"), rec.get("title"), rec.get("year"),
                 json.dumps(rec.get("authors") or [], ensure_ascii=False), rec.get("venue"),
                 rec.get("venue_source_id"), rec.get("venue_type"), rec.get("work_type"),
                 rec.get("language"), rec.get("abstract"),
                 rec.get("abstract_source", "openalex") if rec.get("abstract") else None,
                 rec.get("cited_by_count"), json.dumps(rec.get("referenced_works") or []),
                 json.dumps(rec.get("related_works") or []), json.dumps(rec.get("oa") or {}, ensure_ascii=False),
                 rec.get("is_retracted", 0), "", run_id, db.now(),
                 rec.get("kci_id"), rec.get("uci"), rec.get("title_ko"), rec.get("title_en"),
                 rec.get("kci_citation_count"), rec.get("wos_citation_count"), rec.get("db_origin") or "OPENALEX"))
    db.set_membership(con, pid, "CANDIDATE", "PROPOSED", f"검색 수집({run_id})" if run_id else "수집")
    dups = possible_cross_db_duplicates(con, pid, rec)
    if dups:
        for other, conf in dups:
            record_external_ids(con, pid, rec, "REVIEW_DUPLICATE", other, conf, "국내외 DB 간 제목 유사(번역본 가능) — 사람 확인")
    else:
        record_external_ids(con, pid, rec, "MATCHED")
    return pid, True


def review_duplicates(con) -> list[dict]:
    rows = con.execute("""SELECT DISTINCT paper_id, related_paper_id, match_confidence FROM paper_external_ids
                          WHERE match_status='REVIEW_DUPLICATE'""").fetchall()
    out = []
    for r in rows:
        a = con.execute("SELECT paper_id, title, title_en, year, venue, db_origin FROM papers WHERE paper_id=?", (r[0],)).fetchone()
        b = con.execute("SELECT paper_id, title, title_en, year, venue, db_origin FROM papers WHERE paper_id=?", (r[1],)).fetchone()
        if a and b:
            out.append({"a": dict(a), "b": dict(b), "confidence": r[2]})
    return out


def resolve_duplicate(con, a: str, b: str, status: str, note: str):
    con.execute("""UPDATE paper_external_ids SET match_status=?, note=COALESCE(note,'') || ? , checked_at=?
                   WHERE (paper_id=? AND related_paper_id=?) OR (paper_id=? AND related_paper_id=?)""",
                (status, f" / {status}: {note}", db.now(), a, b, b, a))


def find_version_duplicates(con) -> list[dict]:
    """DOI가 다른데 제목·첫 저자가 같은 문헌쌍 = Preprint/출판본 등 판본 중복 후보."""
    rows = con.execute("SELECT paper_id, title, year, authors_json, work_type, doi, canonical_work_id FROM papers").fetchall()
    buckets: dict[str, list] = {}
    for r in rows:
        buckets.setdefault(norm_title(r["title"])[:60], []).append(r)
    pairs = []
    keys = list(buckets)
    # 정확히 같은 제목 버킷 + 유사 제목(0.93 이상) 비교
    for k in keys:
        grp = buckets[k]
        for i in range(len(grp)):
            for j in range(i + 1, len(grp)):
                pairs.append((grp[i], grp[j], 1.0))
    by_year = sorted(rows, key=lambda r: (r["year"] or 0))
    for i, a in enumerate(by_year):
        for b in by_year[i + 1:]:
            if (b["year"] or 0) - (a["year"] or 0) > 3:
                break
            if norm_title(a["title"])[:60] == norm_title(b["title"])[:60]:
                continue
            s = title_sim(a["title"], b["title"])
            if s >= 0.93:
                pairs.append((a, b, s))
    out = []
    for a, b, s in pairs:
        if first_surname(a["authors_json"]) and first_surname(a["authors_json"]) != first_surname(b["authors_json"]):
            continue
        out.append({"a": a["paper_id"], "b": b["paper_id"], "similarity": round(s, 3),
                    "a_type": a["work_type"], "b_type": b["work_type"], "a_year": a["year"], "b_year": b["year"],
                    "a_doi": a["doi"], "b_doi": b["doi"],
                    "already_linked": bool(a["canonical_work_id"] or b["canonical_work_id"])})
    return out


def link_versions(con, keep: str, other: str, note: str):
    con.execute("UPDATE papers SET canonical_work_id=?, version_note=? WHERE paper_id=?", (keep, note, other))
    db.log_event(con, "link-versions", {"keep": keep, "other": other, "note": note})


def crossref_verify(con, cr: Crossref, pid: str) -> dict:
    p = con.execute("SELECT * FROM papers WHERE paper_id=?", (pid,)).fetchone()
    res = {"paper_id": pid, "doi": p["doi"]}
    if not p["doi"]:
        res["result"] = "NO_DOI"
        con.execute("UPDATE papers SET bib_match=? WHERE paper_id=?", ("NO_DOI", pid))
        return res
    m = cr.work(p["doi"])
    if not m:
        res["result"] = "DOI_NOT_FOUND"
        con.execute("UPDATE papers SET bib_match=?, crossref_checked_at=? WHERE paper_id=?",
                    ("DOI_NOT_FOUND", db.now(), pid))
        return res
    cr_title = (m.get("title") or [""])[0]
    cr_year = None
    for k in ("published-print", "published-online", "issued", "published"):
        dp = (m.get(k) or {}).get("date-parts")
        if dp and dp[0] and dp[0][0]:
            cr_year = dp[0][0]
            break
    cr_first = ((m.get("author") or [{}])[0].get("family") or "")
    sim = title_sim(cr_title, p["title"])
    year_ok = cr_year is None or p["year"] is None or abs(int(cr_year) - int(p["year"])) <= 1
    author_ok = (not cr_first) or norm_title(cr_first) in norm_title(" ".join(
        a.get("name") or "" for a in json.loads(p["authors_json"] or "[]")))
    verdict = "MATCH" if sim >= 0.85 and year_ok and author_ok else "MISMATCH"
    res.update({"result": verdict, "crossref_title": cr_title, "title_similarity": round(sim, 3),
                "crossref_year": cr_year, "year_ok": year_ok, "first_author_ok": author_ok})
    keep = {k: m.get(k) for k in ("DOI", "title", "container-title", "author", "issued", "type", "volume",
                                   "issue", "page", "publisher", "updated-by", "relation", "license", "URL")}
    con.execute("UPDATE papers SET crossref_json=?, crossref_checked_at=?, bib_match=? WHERE paper_id=?",
                (json.dumps(keep, ensure_ascii=False), db.now(), verdict, pid))
    return res


_STATUS_RANK = {"RETRACTED": 4, "EXPRESSION_OF_CONCERN": 3, "CORRECTED": 2, "NO_KNOWN_ALERT": 1, "UNKNOWN": 0}


def integrity_check(con, cr: Crossref, pid: str, oa: OpenAlex | None = None) -> dict:
    """Crossref updated-by(+Retraction Watch), updates 필터, OpenAlex is_retracted를 종합.
    NO_KNOWN_ALERT는 '알려진 경고 없음'일 뿐 철회가 없다는 보증이 아니다.
    철회 공지는 첫 조회 뒤에 나올 수 있으므로 검색 캐시와 달리 INTEGRITY_MAX_AGE 안의 응답만 쓴다."""
    p = con.execute("SELECT * FROM papers WHERE paper_id=?", (pid,)).fetchone()
    alerts, sources = [], []
    status = "UNKNOWN"
    if oa is not None and p["openalex_id"]:
        try:
            w = oa.work(p["openalex_id"], max_age=INTEGRITY_MAX_AGE)
            if w is not None:
                flag = 1 if w.get("is_retracted") else 0
                if flag != p["is_retracted_openalex"]:
                    con.execute("UPDATE papers SET is_retracted_openalex=? WHERE paper_id=?", (flag, pid))
                    p = con.execute("SELECT * FROM papers WHERE paper_id=?", (pid,)).fetchone()
        except Exception as e:  # 갱신 실패 시 저장된 값으로 계속
            alerts.append({"type": "check_error", "detail": "openalex: " + str(e)[:200]})
    if p["is_retracted_openalex"]:
        alerts.append({"type": "retraction", "source": "openalex.is_retracted"})
    if p["doi"]:
        try:
            m = cr.work(p["doi"], max_age=INTEGRITY_MAX_AGE) or {}
            sources.append("crossref.updated-by")
            for u in m.get("updated-by") or []:
                alerts.append({"type": u.get("type"), "notice_doi": u.get("DOI"), "source": u.get("source"),
                               "date": ((u.get("updated") or {}).get("date-time"))})
            for n in cr.notices_updating(p["doi"], max_age=INTEGRITY_MAX_AGE):
                for u in n.get("update-to") or []:
                    if norm_doi(u.get("DOI")) == p["doi"]:
                        alerts.append({"type": u.get("type"), "notice_doi": n.get("DOI"), "source": "crossref.updates"})
            sources.append("crossref.updates")
            rel = m.get("relation") or {}
            note = p["version_note"] or ""
            for k in ("has-preprint", "is-preprint-of", "has-version", "is-version-of"):
                if rel.get(k):
                    item = f" {k}:" + ",".join(x.get("id", "") for x in rel[k])
                    if item not in note:  # 재점검 시 같은 항목이 중복 누적되지 않게
                        note += item
            if note != (p["version_note"] or ""):
                con.execute("UPDATE papers SET version_note=? WHERE paper_id=?", (note, pid))
            status = "NO_KNOWN_ALERT"
        except Exception as e:  # 네트워크 오류 등 → UNKNOWN 유지
            alerts.append({"type": "check_error", "detail": str(e)[:200]})
    sources.append("openalex")
    for a in alerts:
        t = (a.get("type") or "").lower().replace("-", "_").replace(" ", "_")
        if "retract" in t or "withdraw" in t or "removal" in t:
            cand = "RETRACTED"
        elif "concern" in t:
            cand = "EXPRESSION_OF_CONCERN"
        elif t in ("correction", "erratum", "corrigendum", "addendum", "clarification") or "correct" in t:
            cand = "CORRECTED"
        else:
            continue
        if _STATUS_RANK[cand] > _STATUS_RANK.get(status, 0):
            status = cand
    if not p["doi"] and status == "UNKNOWN" and not alerts:
        status = "UNKNOWN"  # DOI가 없어 Crossref 조회 불가
    follow = 1 if status in ("RETRACTED", "EXPRESSION_OF_CONCERN", "CORRECTED", "UNKNOWN") else 0
    con.execute("INSERT INTO publication_updates(paper_id, status, source, detail, checked_at, follow_up_required) VALUES (?,?,?,?,?,?)",
                (pid, status, ",".join(sources), json.dumps(alerts, ensure_ascii=False), db.now(), follow))
    return {"paper_id": pid, "status": status, "alerts": alerts}
