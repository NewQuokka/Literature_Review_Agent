"""원문 확보(F05)·텍스트 추출·인용 대조(F07).

원칙: OpenAlex/Semantic Scholar가 공개(OA)로 표시한 위치의 PDF만 받는다.
랜딩 페이지 스크래핑·로그인·유료벽 우회는 하지 않는다. 실패는 사유와 함께 사용자 요청목록으로 넘긴다.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import unicodedata
import zipfile
from pathlib import Path

from . import db
from .config import Paths, USER_AGENT
from .papers import norm_title

try:
    import pymupdf  # noqa
except ImportError:  # 구버전
    import fitz as pymupdf  # type: ignore

MIN_PDF_BYTES = 8_000
TITLE_MATCH_MIN = 0.6


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(p) -> str:
    authors = json.loads(p["authors_json"] or "[]")
    sur = (authors[0]["name"].split()[-1] if authors and authors[0].get("name") else "unknown")
    sur = re.sub(r"[^\w]", "", unicodedata.normalize("NFKD", sur).encode("ascii", "ignore").decode()) or "unknown"
    return f"{p['paper_id']}_{sur}_{p['year'] or 'nd'}"


def extract_pages(pdf: Path, outdir: Path) -> int:
    outdir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(str(pdf))
    n = 0
    for i, page in enumerate(doc, 1):
        (outdir / f"p{i:03d}.txt").write_text(page.get_text("text"), encoding="utf-8")
        n = i
    doc.close()
    return n


def extract_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    import html
    return html.unescape(text)


def title_match_score(text: str, title: str) -> float:
    words = [w for w in norm_title(title).split() if len(w) > 3]
    if not words:
        return 1.0
    t = norm_title(text)
    return sum(1 for w in words if w in t) / len(words)


def candidate_urls(p, s2=None) -> list[dict]:
    oa = json.loads(p["oa_json"] or "{}")
    urls, seen = [], set()

    def add(url, kind, version=None, license=None, source=None):
        if url and url not in seen:
            seen.add(url)
            urls.append({"url": url, "kind": kind, "version": version, "license": license, "source": source})

    best = oa.get("best") or {}
    add(best.get("pdf_url"), "openalex_best_oa", best.get("version"), best.get("license"))
    for loc in oa.get("locations") or []:
        add(loc.get("pdf_url"), "openalex_oa_location", loc.get("version"), loc.get("license"), loc.get("source"))
    if s2 is not None and p["doi"]:
        try:
            sp = s2.paper(p["doi"]) or {}
            oap = sp.get("openAccessPdf") or {}
            if oap.get("url") and (oap.get("status") or "").upper() != "CLOSED":
                add(oap["url"], "semanticscholar_oa", None, oap.get("license"))
        except Exception:
            pass
    return urls


def landing_pages(p) -> list[str]:
    oa = json.loads(p["oa_json"] or "{}")
    pages = []
    for loc in [oa.get("best") or {}] + (oa.get("locations") or []):
        if loc.get("landing_page_url") and loc["landing_page_url"] not in pages:
            pages.append(loc["landing_page_url"])
    if oa.get("oa_url") and oa["oa_url"] not in pages:
        pages.append(oa["oa_url"])
    return pages


def acquire(con, paths: Paths, http_session, pid: str, s2=None) -> dict:
    p = con.execute("SELECT * FROM papers WHERE paper_id=?", (pid,)).fetchone()
    mem = db.membership(con, pid)
    tier_dir = "Extended" if mem and mem["tier"] == "EXTENDED" else "Core"
    existing = con.execute("SELECT * FROM fulltexts WHERE paper_id=?", (pid,)).fetchone()
    if existing and existing["access_status"] in ("FULL_PDF", "FULL_XML") and Path(existing["file_path"]).exists():
        return {"paper_id": pid, "status": existing["access_status"], "skipped": "이미 확보됨"}
    attempts = []
    for c in candidate_urls(p, s2):
        a = {"url": c["url"], "kind": c["kind"]}
        try:
            r = http_session.get(c["url"], timeout=60, allow_redirects=True, stream=True,
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/pdf"})
            a["http"] = r.status_code
            a["content_type"] = r.headers.get("Content-Type", "")
            if r.status_code != 200:
                a["result"] = f"HTTP {r.status_code}"
                attempts.append(a)
                continue
            data = b""
            for chunk in r.iter_content(1 << 16):
                data += chunk
                if len(data) > 80 * 1024 * 1024:
                    break
            if not data.startswith(b"%PDF"):
                a["result"] = "PDF 아님(HTML/차단 페이지 가능)"
                attempts.append(a)
                continue
            if len(data) < MIN_PDF_BYTES:
                a["result"] = f"파일 너무 작음({len(data)}B)"
                attempts.append(a)
                continue
            tmp = paths.quarantine / f"{pid}.pdf"
            tmp.write_bytes(data)
            try:
                doc = pymupdf.open(str(tmp))
                first = "".join(doc[i].get_text("text") for i in range(min(2, len(doc))))
                npages = len(doc)
                doc.close()
            except Exception as e:
                a["result"] = f"PDF 열기 실패: {e}"
                attempts.append(a)
                continue
            score = title_match_score(first, p["title"] or "")
            a["title_match"] = round(score, 2)
            if score < TITLE_MATCH_MIN:
                a["result"] = f"제목 불일치(다른 문서 의심, 격리: state/quarantine/{pid}.pdf)"
                attempts.append(a)
                continue
            dest = paths.library / tier_dir / f"{safe_name(p)}.pdf"
            shutil.move(str(tmp), dest)
            tdir = paths.text / pid
            if tdir.exists():
                shutil.rmtree(tdir)
            extract_pages(dest, tdir)
            a["result"] = "OK"
            attempts.append(a)
            con.execute("""INSERT OR REPLACE INTO fulltexts(paper_id, file_path, source_url, source_type, version, license,
                           checksum, size_bytes, pages, title_match, access_status, attempts_json, acquired_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (pid, str(dest), r.url, c["kind"], c["version"], c["license"], sha256(dest), len(data),
                         npages, score, "FULL_PDF", json.dumps(attempts, ensure_ascii=False), db.now()))
            return {"paper_id": pid, "status": "FULL_PDF", "file": str(dest), "pages": npages, "license": c["license"]}
        except Exception as e:
            a["result"] = f"오류: {str(e)[:150]}"
            attempts.append(a)
    has_abs = bool(p["abstract"])
    if attempts:
        status = "DOWNLOAD_FAILED"
    elif landing_pages(p):
        status = "REQUEST_USER"
    else:
        status = "REQUEST_USER"
    note = {"attempts": attempts, "landing_pages": landing_pages(p), "has_abstract": has_abs}
    con.execute("""INSERT OR REPLACE INTO fulltexts(paper_id, file_path, source_url, source_type, version, license,
                   checksum, size_bytes, pages, title_match, access_status, attempts_json, acquired_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, None, None, None, None, None, None, None, None, None, status,
                 json.dumps(note, ensure_ascii=False), db.now()))
    return {"paper_id": pid, "status": status, "attempts": attempts}


def ingest_user_file(con, paths: Paths, src: Path, pid: str | None) -> dict:
    """사용자가 제공한 원문 PDF를 P-ID와 연결해 ABSTRACT_ONLY/REQUEST_USER → FULL_PDF 로 승격."""
    if src.suffix.lower() != ".pdf":
        raise SystemExit("현재 PDF만 지원합니다.")
    doc = pymupdf.open(str(src))
    first = "".join(doc[i].get_text("text") for i in range(min(3, len(doc))))
    npages = len(doc)
    doc.close()
    if not pid:
        m = re.search(r"P-\d{3,}", src.name)
        if m:
            pid = m.group(0)
    if not pid:
        dois = set(d.lower().rstrip(".);,") for d in re.findall(r"10\.\d{4,9}/[^\s\"<>]+", first))
        for d in dois:
            r = con.execute("SELECT paper_id FROM papers WHERE doi=?", (d,)).fetchone()
            if r:
                pid = r[0]
                break
    if not pid:
        best, best_s = None, 0.0
        for r in con.execute("""SELECT p.paper_id, p.title FROM papers p JOIN paper_memberships m USING(paper_id)
                                WHERE m.tier IN ('CORE','EXTENDED')"""):
            s = title_match_score(first, r["title"] or "")
            if s > best_s:
                best, best_s = r["paper_id"], s
        if best_s >= 0.8:
            pid = best
    if not pid:
        raise SystemExit(f"{src.name}: 어떤 P-ID와도 매칭되지 않았습니다. --paper P-xxx 로 지정하세요.")
    p = con.execute("SELECT * FROM papers WHERE paper_id=?", (pid,)).fetchone()
    if not p:
        raise SystemExit(f"{pid} 없음")
    score = title_match_score(first, p["title"] or "")
    checksum = sha256(src)
    dup = con.execute("SELECT paper_id FROM fulltexts WHERE checksum=?", (checksum,)).fetchone()
    if dup and dup[0] != pid:
        raise SystemExit(f"같은 파일이 이미 {dup[0]}에 연결되어 있습니다.")
    mem = db.membership(con, pid)
    tier_dir = "Extended" if mem and mem["tier"] == "EXTENDED" else "Core"
    dest = paths.library / tier_dir / f"{safe_name(p)}.pdf"
    shutil.copy2(src, dest)            # 원본(raw/)은 건드리지 않고 복사
    tdir = paths.text / pid
    if tdir.exists():
        shutil.rmtree(tdir)
    extract_pages(dest, tdir)
    con.execute("""INSERT OR REPLACE INTO fulltexts(paper_id, file_path, source_url, source_type, version, license,
                   checksum, size_bytes, pages, title_match, access_status, attempts_json, acquired_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, str(dest), str(src), "user_provided", None, "사용자 제공(재배포 금지)", checksum,
                 dest.stat().st_size, npages, score, "FULL_PDF", "[]", db.now()))
    # 초록 기반 근거는 재검증 대상으로 표시
    con.execute("""UPDATE evidence SET verification_note=COALESCE(verification_note,'') || ' [원문 확보 → 재검증 필요]'
                   WHERE paper_id=? AND verification_status IN ('ABSTRACT_BASED','METADATA_ONLY','UNVERIFIED')""", (pid,))
    warn = None if score >= TITLE_MATCH_MIN else f"제목 일치도 낮음({score:.2f}) — 파일이 맞는지 확인 필요"
    return {"paper_id": pid, "file": str(dest), "pages": npages, "title_match": round(score, 2), "warning": warn}


# ---------- 인용 대조 ----------
_LIG = {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"}


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    for k, v in _LIG.items():
        s = s.replace(k, v)
    s = re.sub(r"[‘’‛′]", "'", s)
    s = re.sub(r"[“”‟″]", '"', s)
    s = re.sub(r"[‐-―−]", "-", s)
    s = re.sub(r"-\s*\n\s*", "", s)          # 줄 끝 하이픈 분철
    s = re.sub(r"\s+", " ", s).lower()
    return s.strip()


def _squash(s: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", s)


def page_text(paths: Paths, pid: str, page: int) -> str | None:
    f = paths.text / pid / f"p{page:03d}.txt"
    return f.read_text(encoding="utf-8") if f.exists() else None


def locate_quote(paths: Paths, pid: str, quote: str) -> list[int]:
    q = _squash(norm_text(quote))
    hits = []
    d = paths.text / pid
    if not d.exists() or not q:
        return hits
    files = sorted(d.glob("p*.txt"))
    for i, f in enumerate(files):
        t = _squash(norm_text(f.read_text(encoding="utf-8")))
        if q in t:
            hits.append(int(f.stem[1:]))
        elif i + 1 < len(files):  # 쪽 경계에 걸친 인용
            t2 = t[-len(q) * 2:] + _squash(norm_text(files[i + 1].read_text(encoding="utf-8")))[:len(q) * 2]
            if q in t2:
                hits.append(int(f.stem[1:]))
    return hits


def verify_evidence_item(con, paths: Paths, ev: dict) -> tuple[str, str]:
    """인용·위치를 실제 원문과 대조해 검증상태를 결정한다(모델의 자기신고를 믿지 않음)."""
    pid = ev["paper_id"]
    fts = db.fulltext_status(con, pid)
    quote = (ev.get("verbatim_short_quote") or "").strip()
    page = ev.get("pdf_page")
    p = con.execute("SELECT abstract FROM papers WHERE paper_id=?", (pid,)).fetchone()
    if fts in ("FULL_PDF", "FULL_XML"):
        if not quote:
            if page:
                t = page_text(paths, pid, int(page))
                if t is None:
                    return "CONFLICT", f"p.{page}는 원문 쪽수 범위를 벗어남"
            return "UNVERIFIED", "직접 인용 없이 재서술만 있음 — 위치는 사람이 확인"
        hits = locate_quote(paths, pid, quote)
        if not hits:
            return "UNVERIFIED", "원문 텍스트에서 인용문을 찾지 못함(추출 오류 또는 부정확한 인용)"
        if page and int(page) in hits:
            return "VERIFIED_FULLTEXT", f"PDF p.{page}에서 글자 대조 일치"
        if page:
            return "CONFLICT", f"인용문은 PDF p.{','.join(map(str, hits))}에 있으나 기재 쪽은 p.{page}"
        return "VERIFIED_FULLTEXT", f"PDF p.{','.join(map(str, hits))}에서 발견(쪽 미기재 → 자동 보정)"
    # 원문 미확보
    if quote and p and p["abstract"] and _squash(norm_text(quote)) in _squash(norm_text(p["abstract"])):
        return "ABSTRACT_BASED", "초록에서 일치 확인(원문 쪽수 없음)"
    if quote:
        return "UNVERIFIED", "원문 미확보 상태의 인용문 — 사용 금지, 원문 확보 후 재검증"
    if p and p["abstract"]:
        return "ABSTRACT_BASED", "초록 기반 재서술"
    return "METADATA_ONLY", "서지정보만 확보"
