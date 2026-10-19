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
TITLE_MATCH_LINK = 0.8        # 사용자 PDF를 제목만으로 자동 연결할 때의 기준
QUOTE_MIN_TOKENS = 4          # 직접 인용 대조에 필요한 최소 어절 수(#3)
QUOTE_MIN_SEGMENT_TOKENS = 2  # 생략부호로 나눈 각 구간의 최소 어절 수
QUOTE_MIN_CJK_CHARS = 8       # 띄어쓰기 없는 한자·가나 인용의 최소 글자 수
QUOTE_MAX_GAP = 1500          # 생략부호 구간 사이에 허용하는 최대 간격(정규화 글자 수)


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


_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uac00-\ud7a3]")
_STOPWORDS = {"the", "and", "for", "with", "from", "into", "of", "in", "on", "to", "by", "at", "as", "or", "an",
              "its", "their", "between", "under", "over", "via", "how", "what", "why", "does", "do", "is", "are"}
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+")


def title_match_score(text: str, title: str) -> float | None:
    """원문 앞부분과 서지 제목의 일치도(0~1). 판정할 근거가 부족하면 None — 자동 연결에 쓰지 않는다(#5).
    한글·한자·가나가 든 제목은 띄어쓰기를 뺀 음절 2-gram, 그 밖은 단어 경계를 지킨 내용어로 대조한다."""
    nt = norm_title(title)
    tx = norm_title(re.sub(r"-\s*\n\s*", "", text or ""))   # 줄 끝 하이픈 분철 복원
    if not nt:
        return None
    if _CJK_RE.search(nt):
        a, b = nt.replace(" ", ""), tx.replace(" ", "")
        grams = {a[i:i + 2] for i in range(len(a) - 1)}
        if len(grams) < 3:
            return None
        have = {b[i:i + 2] for i in range(len(b) - 1)}
        return len(grams & have) / len(grams)
    words = [w for w in nt.split() if len(w) >= 3 and w not in _STOPWORDS]
    if len(words) < 2:
        return None
    tokens = set(tx.split())
    return sum(1 for w in words if w in tokens) / len(words)


def find_dois(text: str) -> list[str]:
    """본문에서 찾은 DOI(소문자, 나온 순서, 중복 제거)."""
    out = []
    for d in _DOI_RE.findall(text or ""):
        d = d.lower().rstrip(".);,")
        if d not in out:
            out.append(d)
    return out


def identity_check(page1: str, first: str, title: str, doi: str | None) -> tuple[float | None, str | None]:
    """내려받은 PDF가 그 논문인지 판정. (제목 일치도, 거부 사유 또는 None)."""
    score = title_match_score(first, title)
    p1 = find_dois(page1)
    own_on_p1 = bool(doi) and doi in p1
    if score is None:
        if own_on_p1 and len(p1) <= 2:
            return None, None
        return None, "제목으로 판정 불가(제목이 너무 짧음)이고 1쪽에서 이 논문의 DOI도 확인되지 않음"
    if score < TITLE_MATCH_MIN:
        return score, f"제목 불일치({score:.2f})"
    if doi and p1 and not own_on_p1 and score < TITLE_MATCH_LINK:
        return score, f"1쪽 DOI({', '.join(p1[:3])})가 이 논문의 DOI와 다르고 제목 일치도도 낮음({score:.2f})"
    return score, None


def pid_from_filename(name: str) -> str | None:
    """파일명의 P-ID(대소문자 무관, P-7·P007·p-007 모두 P-007)."""
    m = re.search(r"(?<![A-Za-z])[Pp]-?(\d{1,6})(?!\d)", name)
    return f"P-{int(m.group(1)):03d}" if m else None


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
                pages = [doc[i].get_text("text") for i in range(min(2, len(doc)))]
                npages = len(doc)
                doc.close()
            except Exception as e:
                a["result"] = f"PDF 열기 실패: {e}"
                attempts.append(a)
                continue
            score, why = identity_check(pages[0] if pages else "", "".join(pages), p["title"] or "", p["doi"])
            a["title_match"] = round(score, 2) if score is not None else None
            if why:
                a["result"] = f"{why} — 다른 문서 의심, 격리: state/quarantine/{pid}.pdf"
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
    pages = [doc[i].get_text("text") for i in range(min(3, len(doc)))]
    npages = len(doc)
    doc.close()
    first = "".join(pages)
    if not pid:
        cand = pid_from_filename(src.name)
        if cand and con.execute("SELECT 1 FROM papers WHERE paper_id=?", (cand,)).fetchone():
            pid = cand
    if not pid:
        # 1쪽의 DOI를 우선하고, 없으면 앞 3쪽. DB와 맞는 DOI가 둘 이상이면(인용한 다른 논문일 수 있음) 자동 연결하지 않는다
        for scope in (pages[:1], pages):
            hits = []
            for d in find_dois("".join(scope)):
                r = con.execute("SELECT paper_id FROM papers WHERE doi=?", (d,)).fetchone()
                if r and r[0] not in hits:
                    hits.append(r[0])
            if len(hits) > 1:
                raise SystemExit(f"{src.name}: 여러 논문의 DOI가 보입니다({', '.join(hits)}). --paper P-xxx 로 지정하세요.")
            if hits:
                pid = hits[0]
                break
    if not pid:
        scored = []
        for r in con.execute("""SELECT p.paper_id, p.title FROM papers p JOIN paper_memberships m USING(paper_id)
                                WHERE m.tier IN ('CORE','EXTENDED')"""):
            s = title_match_score(first, r["title"] or "")
            if s is not None and s >= TITLE_MATCH_LINK:
                scored.append((s, r["paper_id"]))
        if len(scored) > 1:
            raise SystemExit(f"{src.name}: 제목이 맞는 후보가 여럿입니다({', '.join(x[1] for x in scored)}). --paper P-xxx 로 지정하세요.")
        if scored:
            pid = scored[0][1]
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
    if score is None:
        warn = "제목이 짧아 일치도를 판정할 수 없음 — 파일이 맞는지 사람이 확인"
    else:
        warn = None if score >= TITLE_MATCH_MIN else f"제목 일치도 낮음({score:.2f}) — 파일이 맞는지 확인 필요"
    return {"paper_id": pid, "file": str(dest), "pages": npages,
            "title_match": round(score, 2) if score is not None else None, "warning": warn}


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


# 대조할 때 남기는 기호: 수치의 뜻을 바꾸는 것들. 글자는 \w(모든 문자 체계)로 남는다(#3)
_DROP_RE = re.compile(r"[^\w.,\-+<>=≤≥±%&/×]|_")
_ELLIPSIS_RE = re.compile(r"\[\s*\.\s?\.\s?\.\s*\]|\.\s?\.\s?\.")


def _squash(s: str) -> str:
    """norm_text 결과에서 공백·따옴표·괄호 등 표기 차이만 지운다. 소수점·부호·부등호·% 등은 남긴다."""
    return _DROP_RE.sub("", s).rstrip(".,").lstrip(",")


def _tokens(s: str) -> int:
    return len(re.findall(r"\w+", s))


def quote_segments(quote: str) -> tuple[list[str], str | None]:
    """직접 인용을 생략부호 기준 구간으로 나눠 정규화한다. (구간들, 대조 불가 사유 또는 None)."""
    norm = norm_text(quote)
    parts = [p for p in _ELLIPSIS_RE.split(norm) if _squash(p)]
    if not parts:
        return [], "정규화 후 남는 글자가 없음"
    cjk_only = not re.search(r"\s", norm.strip()) and _CJK_RE.search(norm) and not re.search(r"[가-힣]", norm)
    if cjk_only:
        if len(_squash(norm)) < QUOTE_MIN_CJK_CHARS:
            return [], f"인용문이 너무 짧음({QUOTE_MIN_CJK_CHARS}자 이상 필요)"
    elif sum(_tokens(p) for p in parts) < QUOTE_MIN_TOKENS:
        return [], f"인용문이 너무 짧음({QUOTE_MIN_TOKENS}어절 이상 필요)"
    elif len(parts) > 1 and any(_tokens(p) < QUOTE_MIN_SEGMENT_TOKENS for p in parts):
        return [], f"생략부호로 나눈 구간이 너무 짧음(구간마다 {QUOTE_MIN_SEGMENT_TOKENS}어절 이상 필요)"
    return [_squash(p) for p in parts], None


def find_segments(text: str, segs: list[str], max_gap: int = QUOTE_MAX_GAP) -> list[tuple[int, int]]:
    """정규화한 텍스트에서 구간들이 순서대로(간격 max_gap 이내) 나오는 위치 (시작, 끝) 목록."""
    out, i = [], text.find(segs[0])
    while i >= 0:
        pos, ok = i + len(segs[0]), True
        for s in segs[1:]:
            j = text.find(s, pos)
            if j < 0 or j - pos > max_gap:
                ok = False
                break
            pos = j + len(s)
        if ok:
            out.append((i, pos))
        i = text.find(segs[0], i + 1)
    return out


def page_text(paths: Paths, pid: str, page: int) -> str | None:
    f = paths.text / pid / f"p{page:03d}.txt"
    return f.read_text(encoding="utf-8") if f.exists() else None


def locate_quote(paths: Paths, pid: str, quote: str) -> list[int]:
    """인용문이 있는 PDF 쪽 번호. 쪽 경계에 걸친 인용은 시작 쪽으로 센다. 대조 불가(너무 짧음 등)면 빈 목록."""
    segs, _ = quote_segments(quote)
    hits = []
    d = paths.text / pid
    if not d.exists() or not segs:
        return hits
    files = sorted(d.glob("p*.txt"))
    texts = [_squash(norm_text(f.read_text(encoding="utf-8"))) for f in files]
    for i, f in enumerate(files):
        if find_segments(texts[i], segs):
            hits.append(int(f.stem[1:]))
        elif i + 1 < len(files):  # 쪽 경계에 걸친 인용 — 경계를 실제로 가로지르는 일치만
            joined = texts[i] + texts[i + 1]
            if any(s < len(texts[i]) < e for s, e in find_segments(joined, segs)):
                hits.append(int(f.stem[1:]))
    return hits


def verified_page(paths: Paths, pid: str, quote: str) -> int | None:
    """쪽 미기재 인용이 한 쪽에서만 발견됐을 때 그 쪽(자동 보정값을 pdf_page에 저장하기 위함)."""
    hits = locate_quote(paths, pid, quote) if quote else []
    return hits[0] if len(hits) == 1 else None


def text_drift(con, paths: Paths, pid: str) -> list[int] | None:
    """state/text/의 쪽별 텍스트가 PDF에서 새로 추출한 것과 다른 쪽 번호. PDF가 없으면 None.
    대조 기준 텍스트가 고쳐지면 인용 대조가 무의미해지므로 verify가 PDF와 다시 맞춰 본다(#4)."""
    r = con.execute("SELECT file_path FROM fulltexts WHERE paper_id=? AND access_status='FULL_PDF'", (pid,)).fetchone()
    if not r or not r[0] or not Path(r[0]).exists():
        return None
    d = paths.text / pid
    doc = pymupdf.open(r[0])
    try:
        fresh = [page.get_text("text") for page in doc]
    finally:
        doc.close()
    have = sorted(d.glob("p*.txt")) if d.exists() else []
    bad = [i for i, t in enumerate(fresh, 1)
           if not (d / f"p{i:03d}.txt").exists() or (d / f"p{i:03d}.txt").read_text(encoding="utf-8") != t]
    bad += [int(f.stem[1:]) for f in have if int(f.stem[1:]) > len(fresh)]   # PDF에 없는 쪽 파일
    return bad


def reextract(con, paths: Paths, pid: str) -> bool:
    """PDF에서 쪽별 텍스트를 다시 만든다. PDF가 없으면 False."""
    r = con.execute("SELECT file_path FROM fulltexts WHERE paper_id=? AND access_status='FULL_PDF'", (pid,)).fetchone()
    if not r or not r[0] or not Path(r[0]).exists():
        return False
    tdir = paths.text / pid
    if tdir.exists():
        shutil.rmtree(tdir)
    extract_pages(Path(r[0]), tdir)
    return True


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
        segs, why = quote_segments(quote)
        if why:
            return "UNVERIFIED", f"대조 불가: {why}"
        hits = locate_quote(paths, pid, quote)
        if not hits:
            return "UNVERIFIED", "원문 텍스트에서 인용문을 찾지 못함(추출 오류 또는 부정확한 인용)"
        if page and int(page) in hits:
            return "VERIFIED_FULLTEXT", f"PDF p.{page}에서 글자 대조 일치"
        if page:
            return "CONFLICT", f"인용문은 PDF p.{','.join(map(str, hits))}에 있으나 기재 쪽은 p.{page}"
        if len(hits) > 1:
            return "UNVERIFIED", f"인용문이 여러 쪽(PDF p.{','.join(map(str, hits))})에 있음 — 쪽을 기재해야 확인됨"
        return "VERIFIED_FULLTEXT", f"PDF p.{hits[0]}에서 발견(쪽 미기재 → 자동 보정)"
    # 원문 미확보
    segs, why = quote_segments(quote) if quote else ([], None)
    if segs and p and p["abstract"] and find_segments(_squash(norm_text(p["abstract"])), segs):
        return "ABSTRACT_BASED", "초록에서 일치 확인(원문 쪽수 없음)"
    if quote:
        return "UNVERIFIED", "원문 미확보 상태의 인용문 — 사용 금지, 원문 확보 후 재검증"
    if p and p["abstract"]:
        return "ABSTRACT_BASED", "초록 기반 재서술"
    return "METADATA_ONLY", "서지정보만 확보"
