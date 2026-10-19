"""논문별 중요도 점수·순위(v1.3, V16).

중요도 = 특정 Research Profile 버전에서의 '상대적 활용 우선순위'. 학술 가치의 절대 서열이 아니다.
- 기준 5개를 에이전트가 0~5점으로 잠정 평가(근거 필수) → 점수 = Σ(가중치 × 점수) × 20 (0~100).
- 근거가 없는 기준은 빈칸 → 점수를 만들지 않고 PENDING_EVIDENCE(평가보류).
- 인용수·학술지 지표는 점수에 넣지 않는다(보조 표시만).
- 동점: 연구질문 직접 관련성 → 역할 기여도 → 근거 적절성 → 사용자 승인 순으로 가르고, 그래도 같으면 공동 순위.
- 사용자 고정(Pin)은 AI 제안 순위와 별도로 남긴다.
"""
from __future__ import annotations

import json

from . import db

TIERS = ("A", "B", "C")
TIER_KO = {"A": "A 핵심 활용", "B": "B 적극 참고", "C": "C 보완 참고", "평가보류": "평가보류"}


PROVISIONAL_CRITERION = "evidence_quality"   # 초록만으로는 판단하기 어려워 비워 둘 수 있는 기준


def compute_score(scores: dict, weights: dict) -> float | None:
    """0~100 점수. 근거 적절성만 비었으면 나머지 기준의 가중치로 정규화한 '잠정' 점수(#9).
    다른 기준이 비면 None(평가보류)."""
    missing = [c for c in db.CRITERIA if scores.get(c) is None]
    for c in db.CRITERIA:
        v = scores.get(c)
        if v is not None and not (0 <= float(v) <= 5):
            raise ValueError(f"{c}={v}: 0~5 범위를 벗어남")
    if not missing:
        return round(sum(weights[c] * float(scores[c]) for c in db.CRITERIA) * 20, 2)
    if missing == [PROVISIONAL_CRITERION]:
        w = sum(weights[c] for c in db.CRITERIA if c != PROVISIONAL_CRITERION)
        if w <= 0:
            return None
        return round(sum(weights[c] * float(scores[c]) for c in db.CRITERIA if c != PROVISIONAL_CRITERION) / w * 20, 2)
    return None


def is_provisional(r) -> bool:
    return r["importance_score"] is not None and r[PROVISIONAL_CRITERION] is None


def _tie_key(r):
    approved = 1 if r["proposed_or_approved"] == "APPROVED" else 0
    return (-(r["importance_score"] or 0), -(r["research_fit"] or 0), -(r["role_contribution"] or 0),
            -(r["evidence_quality"] or 0), -approved)


def _rank_with_ties(rows) -> dict[int, tuple[int, str]]:
    """eval_id → (순위, 표기). 정렬 키가 완전히 같으면 공동 순위('3=')."""
    out, prev, rank = {}, None, 0
    ordered = sorted(rows, key=_tie_key)
    groups = []
    for r in ordered:
        k = _tie_key(r)
        if k != prev:
            groups.append([r])
            prev = k
        else:
            groups[-1].append(r)
    pos = 1
    for g in groups:
        for r in g:
            out[r["eval_id"]] = (pos, f"{pos}=" if len(g) > 1 else str(pos))
        pos += len(g)
    return out


def current_rows(con, scope: str):
    """현재 연구계획 버전에서 해당 집합(CORE/EXTENDED)의 현행 평가(제외된 문헌 빼고).
    이전 버전 기준 평가는 섞지 않는다 — V16이 재평가 필요로 알린다(#9). 순서는 P-ID로 고정."""
    return con.execute("""SELECT r.*, p.role_tags FROM paper_rankings r
                          JOIN paper_memberships m ON m.paper_id=r.paper_id
                          JOIN papers p ON p.paper_id=r.paper_id
                          WHERE r.is_current=1 AND r.set_scope=? AND m.tier=? AND m.status!='REJECTED'
                          AND r.version_id IS ? ORDER BY r.paper_id""",
                       (scope, scope, db.current_version(con))).fetchall()


def recompute(con, scope: str) -> None:
    rows = current_rows(con, scope)
    scored = [r for r in rows if r["importance_score"] is not None]
    pending = [r for r in rows if r["importance_score"] is None]
    ai = _rank_with_ties(scored)
    # 최종 순위: Pin된 문헌을 지정 위치에 두고 나머지를 AI 순서대로 채운다
    pins = {r["eval_id"]: r["pinned_rank"] for r in scored if r["pinned_rank"]}
    unpinned = [r for r in sorted(scored, key=lambda r: ai[r["eval_id"]][0]) if r["eval_id"] not in pins]
    slots: dict[int, int] = {}
    for eid, pr in sorted(pins.items(), key=lambda x: x[1]):
        p = pr
        while p in slots:
            p += 1
        slots[p] = eid
    final, pos, it = {}, 1, iter(unpinned)
    total = len(scored)
    while len(final) < total:
        if pos in slots:
            final[slots[pos]] = pos
        else:
            nxt = next(it, None)
            if nxt is None:  # 남은 Pin(범위 밖 위치)
                for eid in [e for e in slots.values() if e not in final]:
                    final[eid] = pos
                    pos += 1
                break
            final[nxt["eval_id"]] = pos
        pos += 1
    # 표기: Pin이 있어도 고정되지 않은 동점 문헌끼리는 공동 순위(n=)를 유지한다(#9)
    by_final = sorted(scored, key=lambda r: final[r["eval_id"]])
    labels: dict[int, str] = {}
    groups: list[list] = []
    for r in (x for x in by_final if x["eval_id"] not in pins):
        if groups and _tie_key(groups[-1][-1]) == _tie_key(r):
            groups[-1].append(r)
        else:
            groups.append([r])
    for g in groups:
        first = min(final[r["eval_id"]] for r in g)
        for r in g:
            labels[r["eval_id"]] = f"{first}=" if len(g) > 1 else str(final[r["eval_id"]])
    for eid in pins:
        labels[eid] = str(final[eid])
    for r in scored:
        rank = final[r["eval_id"]]
        label = labels[r["eval_id"]] + (" (잠정)" if is_provisional(r) else "")
        con.execute("UPDATE paper_rankings SET ai_rank=?, core_rank=?, rank_label=? WHERE eval_id=?",
                    (ai[r["eval_id"]][0], rank, label, r["eval_id"]))
    for r in pending:
        con.execute("UPDATE paper_rankings SET ai_rank=NULL, core_rank=NULL, rank_label='평가보류' WHERE eval_id=?",
                    (r["eval_id"],))
    # 역할별 순위: 최종 순서를 역할 태그로 걸러 다시 매김(복수 역할 논문은 각 역할에 순위)
    for role in db.ROLES:
        col = f"{role}_rank"
        con.execute(f"UPDATE paper_rankings SET {col}=NULL WHERE eval_id IN ({','.join(str(r['eval_id']) for r in rows) or 0})")
        members = sorted((r for r in scored if role in (r["role_tags"] or "").split(",")), key=lambda r: final[r["eval_id"]])
        prev, prev_rank = None, 0
        for i, r in enumerate(members, 1):
            tie = (prev is not None and prev["eval_id"] not in pins and r["eval_id"] not in pins
                   and _tie_key(prev) == _tie_key(r))
            prev_rank = prev_rank if tie else i   # 역할별 순위에도 같은 동점 규칙
            con.execute(f"UPDATE paper_rankings SET {col}=? WHERE eval_id=?", (prev_rank, r["eval_id"]))
            prev = r


def add_scores(con, items: list[dict], evaluated_by: str = "agent") -> list[dict]:
    wv, weights = db.current_weights(con)
    ver = db.current_version(con)
    pid_ = db.project_id(con)
    res, scopes = [], set()
    for it in items:
        pid = it["paper_id"]
        m = db.membership(con, pid)
        if not m or m["tier"] not in ("CORE", "EXTENDED") or m["status"] == "REJECTED":
            res.append({"paper_id": pid, "rejected": "Core/Extended(제외 아님) 문헌만 순위 대상"})
            continue
        scores = {c: it.get(c) for c in db.CRITERIA}
        rationale = it.get("rationale") or {}
        missing_why = [c for c in db.CRITERIA if scores[c] is not None and not (rationale.get(c) or "").strip()]
        if missing_why:
            res.append({"paper_id": pid, "rejected": f"점수에 근거 문장 없음: {missing_why}"})
            continue
        try:
            score = compute_score(scores, weights)
        except ValueError as e:
            res.append({"paper_id": pid, "rejected": str(e)})
            continue
        prev = con.execute("SELECT * FROM paper_rankings WHERE paper_id=? AND is_current=1", (pid,)).fetchone()
        status = "PENDING_EVIDENCE" if score is None else "PROPOSED"
        tier = it.get("tier") if score is not None else "평가보류"
        if score is not None and tier not in TIERS:
            tier = None
        pinned = prev["pinned_rank"] if prev and prev["set_scope"] == m["tier"] else None
        override = prev["override_reason"] if pinned else None
        con.execute("UPDATE paper_rankings SET is_current=0 WHERE paper_id=? AND is_current=1", (pid,))
        fts = db.fulltext_status(con, pid)
        con.execute("""INSERT INTO paper_rankings(project_id, version_id, paper_id, set_scope, research_fit, role_contribution,
                       evidence_quality, distinctiveness, time_relevance, weight_version, importance_score, importance_tier,
                       rationale_json, uncertainty, basis, evaluated_at, proposed_or_approved, pinned_rank, override_reason,
                       change_reason, evaluated_by, is_current) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                    (pid_, ver, pid, m["tier"], *[scores[c] for c in db.CRITERIA], wv, score, tier,
                     json.dumps(rationale, ensure_ascii=False), it.get("uncertainty"),
                     "FULLTEXT" if fts in ("FULL_PDF", "FULL_XML") else "ABSTRACT",
                     db.now(), "OVERRIDDEN" if pinned else status, pinned, override,
                     it.get("change_reason") or (None if not prev else "재평가"), evaluated_by))
        scopes.add(m["tier"])
        r = {"paper_id": pid, "score": score, "status": status, "tier": tier}
        if score is not None and scores[PROVISIONAL_CRITERION] is None:
            r["provisional"] = "근거 적절성 미평가 — 나머지 기준으로 정규화한 잠정 점수(원문 확인 후 재평가)"
        if score is not None and tier is None:
            r["warning"] = "tier(A/B/C) 미지정 — 점수 분포·역할을 보고 지정하세요"
        if prev:
            r["previous_score"] = prev["importance_score"]
        res.append(r)
    for s in scopes:
        recompute(con, s)
    return res


def pin(con, pid: str, rank: int | None, reason: str | None, by: str = "user") -> dict:
    r = con.execute("SELECT * FROM paper_rankings WHERE paper_id=? AND is_current=1", (pid,)).fetchone()
    if not r:
        raise SystemExit(f"{pid}: 현행 중요도 평가가 없음 — 먼저 `rank score`")
    if rank is not None and not reason:
        raise SystemExit("Pin에는 --reason(사용자 수정 사유)이 필요합니다")
    if rank is not None and rank < 1:
        raise SystemExit(f"--rank는 1 이상이어야 합니다(받은 값 {rank})")
    if rank is not None:
        other = con.execute("""SELECT paper_id FROM paper_rankings WHERE is_current=1 AND set_scope=? AND pinned_rank=?
                               AND paper_id!=?""", (r["set_scope"], rank, pid)).fetchone()
        if other:   # 같은 자리에 두 편을 고정하면 한 편이 말없이 밀려남(#9)
            raise SystemExit(f"{other[0]}가 이미 {rank}위로 고정되어 있습니다 — 먼저 `rank unpin {other[0]}` 또는 다른 순위")
    con.execute("""UPDATE paper_rankings SET pinned_rank=?, override_reason=?, proposed_or_approved=?, evaluated_by=?
                   WHERE eval_id=?""",
                (rank, reason if rank is not None else None,
                 "OVERRIDDEN" if rank is not None else ("PROPOSED" if r["importance_score"] is not None else "PENDING_EVIDENCE"),
                 by if rank is not None else r["evaluated_by"], r["eval_id"]))
    recompute(con, r["set_scope"])
    return dict(con.execute("SELECT paper_id, ai_rank, core_rank, rank_label, pinned_rank, proposed_or_approved FROM paper_rankings WHERE eval_id=?",
                            (r["eval_id"],)).fetchone())


def approve(con, pids: list[str] | None, by: str) -> int:
    q = ("UPDATE paper_rankings SET proposed_or_approved='APPROVED', approved_by=? "
         "WHERE is_current=1 AND proposed_or_approved='PROPOSED'")
    if pids:
        q += f" AND paper_id IN ({','.join('?' * len(pids))})"
        n = con.execute(q, [by, *pids]).rowcount
    else:
        n = con.execute(q, [by]).rowcount
    for s in ("CORE", "EXTENDED"):
        recompute(con, s)
    return n


def set_weights(con, weights: dict, note: str, by: str) -> str:
    if set(weights) != set(db.CRITERIA):
        raise SystemExit(f"가중치 키는 {db.CRITERIA} 다섯 개여야 합니다")
    bad = {k: v for k, v in weights.items() if not (0 <= float(v) <= 1)}
    if bad:   # 합만 보면 음수·1 초과 가중치로 100점을 넘는 점수가 나옴(#9)
        raise SystemExit(f"가중치는 각각 0 이상 1 이하여야 합니다: {bad}")
    if abs(sum(weights.values()) - 1) > 1e-6:
        raise SystemExit(f"가중치 합이 1이어야 합니다(현재 {sum(weights.values())})")
    n = con.execute("SELECT COUNT(*) FROM ranking_weights").fetchone()[0]
    wv = f"W{n + 1}"
    con.execute("INSERT INTO ranking_weights VALUES (?,?,?,?,?)", (wv, json.dumps(weights), note, by, db.now()))
    return wv


def rescore_with_current_weights(con) -> int:
    """가중치 변경 후: 기준 점수는 그대로, 새 가중치로 점수만 재계산한 새 평가행을 만든다(이력 보존)."""
    wv, w = db.current_weights(con)
    rows = con.execute("SELECT * FROM paper_rankings WHERE is_current=1 AND weight_version!=?", (wv,)).fetchall()
    for r in rows:
        item = {c: r[c] for c in db.CRITERIA}
        item.update({"paper_id": r["paper_id"], "rationale": json.loads(r["rationale_json"] or "{}"),
                     "tier": r["importance_tier"] if r["importance_tier"] in TIERS else None,
                     "uncertainty": r["uncertainty"], "change_reason": f"가중치 {r['weight_version']}→{wv}"})
        add_scores(con, [item], r["evaluated_by"] or "agent")
    return len(rows)
