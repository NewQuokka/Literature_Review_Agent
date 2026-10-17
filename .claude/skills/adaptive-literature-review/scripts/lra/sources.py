"""외부 학술 DB 클라이언트: OpenAlex(주), Crossref(서지·정정/철회), Semantic Scholar(보완).

외부로 나가는 것은 검색어·식별자뿐이다. 연구계획서 원문은 보내지 않는다.
"""
from __future__ import annotations

import json
from urllib.parse import quote

from .config import Http, HttpError

OA = "https://api.openalex.org"
CR = "https://api.crossref.org"
S2 = "https://api.semanticscholar.org/graph/v1"

OA_SELECT = ",".join([
    "id", "doi", "display_name", "publication_year", "type", "language", "authorships",
    "primary_location", "best_oa_location", "open_access", "locations", "cited_by_count",
    "referenced_works", "related_works", "is_retracted", "abstract_inverted_index", "ids",
    "primary_topic",
])


def short_oa(wid: str | None) -> str | None:
    if not wid:
        return None
    return wid.rstrip("/").split("/")[-1]


def norm_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip().lower()
    for p in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if d.startswith(p):
            d = d[len(p):]
    return d or None


def search_value(q: str) -> str:
    """OpenAlex filter 값으로 넣을 검색어. 쉼표는 filter 구분자라 공백으로 바꾼다(#10)."""
    return " ".join(q.replace(",", " ").split())


def doi_path(doi: str) -> str:
    """URL 경로에 넣을 DOI. `#`·`?`·`<>` 등이 잘리거나 바뀌지 않게 인코딩한다(#6)."""
    return quote(doi, safe="/")


def abstract_from_inverted(inv: dict | None) -> str | None:
    if not inv:
        return None
    pos = []
    for word, idxs in inv.items():
        for i in idxs:
            pos.append((i, word))
    return " ".join(w for _, w in sorted(pos)) or None


# 심사기록·정오표·철회공지 등 비논문 레코드 기본 제외(--all-types로 해제)
NON_PAPER_FILTER = "type:!peer-review,type:!paratext,type:!erratum,type:!retraction"


class OpenAlex:
    def __init__(self, http: Http, cfg: dict, all_types: bool = False):
        self.http = http
        self.type_filter = "" if all_types else NON_PAPER_FILTER
        self.base = {}
        if cfg.get("openalex_api_key"):
            self.base["api_key"] = cfg["openalex_api_key"]
        if cfg.get("mailto"):
            self.base["mailto"] = cfg["mailto"]
        self.calls = 0        # 실제 네트워크 요청 수(캐시 적중 제외)
        self.cost = 0.0       # OpenAlex가 보고한 비용(USD)

    def _get(self, path: str, params: dict, cache: bool = True, max_age: float | None = None):
        p = {**self.base, **params}
        self.http.last_headers = {}
        body = self.http.get_json(OA + path, p, cache=cache, min_interval=0.12, max_age=max_age)
        h = self.http.last_headers
        if h:  # 캐시 적중이면 비어 있음
            self.calls += 1
            try:
                self.cost += float(h.get("X-RateLimit-Cost-USD") or 0)
            except ValueError:
                pass
        if h.get("X-RateLimit-Remaining-USD"):
            self.remaining_usd = h.get("X-RateLimit-Remaining-USD")
        return body

    # ---- 학술출처(/sources) ----
    SOURCE_SELECT = ("id,display_name,type,issn_l,works_count,is_core,listed_in,is_in_doaj,"
                     "host_organization_name,homepage_url,summary_stats")

    def search_sources(self, query: str, per_page: int = 10, source_type: str | None = None):
        params = {"search": query, "per_page": per_page, "select": self.SOURCE_SELECT}
        if source_type:
            params["filter"] = f"type:{source_type}"
        return self._get("/sources", params)

    def source(self, sid: str):
        """단건 조회(무료) — 출처 ID 실재 확인용."""
        return self._get(f"/sources/{short_oa(sid)}", {"select": self.SOURCE_SELECT})

    def search(self, query: str, field: str = "title_and_abstract", filters: str = "",
               per_page: int = 50, sort: str | None = None, source_ids: list[str] | None = None):
        """field: title_and_abstract | fulltext | title | keyword(search= 기본 검색)."""
        flt = []
        params = {"per_page": min(per_page, 100), "select": OA_SELECT}
        if field == "keyword":
            params["search"] = query
        else:
            flt.append(f"{field}.search:{search_value(query)}")
        if filters:
            flt.append(filters)
        if source_ids:  # Stage A: 우선 학술출처로 제한
            flt.append("primary_location.source.id:" + "|".join(short_oa(x) for x in source_ids))
        if self.type_filter:
            flt.append(self.type_filter)
        if flt:
            params["filter"] = ",".join(flt)
        if sort:
            params["sort"] = sort
        return self._get("/works", params)

    def work(self, ident: str, max_age: float | None = None):
        """W-ID 또는 DOI. 단건 조회는 무료."""
        if ident.startswith("10."):
            ident = "doi:" + doi_path(ident)
        return self._get(f"/works/{ident}", {"select": OA_SELECT}, max_age=max_age)

    def works_by_ids(self, wids: list[str], extra_filter: str = ""):
        out = []
        for i in range(0, len(wids), 100):
            chunk = [short_oa(w) for w in wids[i:i + 100]]
            flt = "openalex_id:" + "|".join(chunk)
            if extra_filter:
                flt += "," + extra_filter
            if self.type_filter:
                flt += "," + self.type_filter
            body = self._get("/works", {"filter": flt, "per_page": 100, "select": OA_SELECT})
            out.extend(body.get("results", []) if body else [])
        return out

    def citing(self, wid: str, query: str = "", per_page: int = 50, filters: str = ""):
        flt = [f"cites:{short_oa(wid)}"]
        if query:
            flt.append(f"title_and_abstract.search:{search_value(query)}")
        if filters:
            flt.append(filters)
        if self.type_filter:
            flt.append(self.type_filter)
        params = {"filter": ",".join(flt), "per_page": min(per_page, 100), "select": OA_SELECT}
        if not query:
            params["sort"] = "cited_by_count:desc"
        return self._get("/works", params)


def oa_to_record(w: dict) -> dict:
    """OpenAlex work → papers 테이블 필드."""
    authors = []
    for a in w.get("authorships") or []:
        au = a.get("author") or {}
        authors.append({"name": au.get("display_name"), "orcid": au.get("orcid"),
                        "position": a.get("author_position")})
    pl = w.get("primary_location") or {}
    src = (pl.get("source") or {}) if pl else {}
    venue = src.get("display_name")
    oa_locs = []
    for loc in w.get("locations") or []:
        if loc.get("is_oa"):
            oa_locs.append({"pdf_url": loc.get("pdf_url"), "landing_page_url": loc.get("landing_page_url"),
                            "version": loc.get("version"), "license": loc.get("license"),
                            "source": (loc.get("source") or {}).get("display_name")})
    best = w.get("best_oa_location") or {}
    oa = {"is_oa": (w.get("open_access") or {}).get("is_oa"),
          "oa_status": (w.get("open_access") or {}).get("oa_status"),
          "oa_url": (w.get("open_access") or {}).get("oa_url"),
          "best": {"pdf_url": best.get("pdf_url"), "landing_page_url": best.get("landing_page_url"),
                   "version": best.get("version"), "license": best.get("license")} if best else None,
          "locations": oa_locs}
    topic = w.get("primary_topic") or {}
    return {
        "doi": norm_doi(w.get("doi")),
        "openalex_id": short_oa(w.get("id")),
        "title": w.get("display_name") or w.get("title"),
        "year": w.get("publication_year"),
        "authors": authors,
        "venue": venue,
        "venue_source_id": short_oa(src.get("id")),
        "venue_type": src.get("type"),
        "work_type": w.get("type"),
        "language": w.get("language"),
        "abstract": abstract_from_inverted(w.get("abstract_inverted_index")),
        "cited_by_count": w.get("cited_by_count"),
        "referenced_works": [short_oa(x) for x in (w.get("referenced_works") or [])],
        "related_works": [short_oa(x) for x in (w.get("related_works") or [])],
        "oa": oa,
        "is_retracted": 1 if w.get("is_retracted") else 0,
        "field": (topic.get("field") or {}).get("display_name") if topic else None,
    }


class Crossref:
    def __init__(self, http: Http, cfg: dict):
        self.http = http
        self.params = {"mailto": cfg["mailto"]} if cfg.get("mailto") else {}

    def work(self, doi: str, max_age: float | None = None):
        body = self.http.get_json(f"{CR}/works/{doi_path(doi)}", dict(self.params), min_interval=0.2, max_age=max_age)
        return (body or {}).get("message") if body else None

    def notices_updating(self, doi: str, max_age: float | None = None):
        """이 DOI를 갱신(정정·철회 등)하는 공지 목록."""
        body = self.http.get_json(f"{CR}/works", {**self.params, "filter": f"updates:{doi}", "rows": 20},
                                  min_interval=0.2, max_age=max_age)
        return ((body or {}).get("message") or {}).get("items", [])


class SemanticScholar:
    def __init__(self, http: Http, cfg: dict):
        self.http = http
        self.headers = {"x-api-key": cfg["s2_api_key"]} if cfg.get("s2_api_key") else {}

    def paper(self, doi: str):
        return self.http.get_json(f"{S2}/paper/DOI:{doi_path(doi)}",
                                  {"fields": "title,abstract,openAccessPdf,externalIds,year,venue"},
                                  headers=self.headers, min_interval=1.1)


# ---------------- KCI Open API (v1.3, 2차) ----------------
# 공식 명세: https://www.kci.go.kr/kciportal/po/openapi/openDataView.kci?datasetBean.dtstSeqNo=1
# 인증키로 실측(`kci probe`)을 통과하기 전에는 검색에 쓰지 않는다. 응답 필드는 명세 기준이며 실측으로 확인한다.
KCI_URL = "https://open.kci.go.kr/po/openapi/openApiSearch.kci"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return c
    return None


def _findall(el, name):
    return [c for c in el.iter() if _local(c.tag) == name]


def _text(el) -> str | None:
    if el is None:
        return None
    t = "".join(el.itertext()).strip()
    return t or None


class KCIError(RuntimeError):
    pass


class KCI:
    def __init__(self, http: Http, cfg: dict):
        self.http = http
        self.key = cfg.get("kci_api_key")
        self.calls = 0

    def _get_xml(self, params: dict, cache: bool = True):
        import xml.etree.ElementTree as ET
        if not self.key:
            raise KCIError("KCI 인증키가 없습니다(환경변수 KCI_API_KEY 또는 state/config.json의 kci_api_key).")
        p = {"key": self.key, **params}
        ck = self.http._key(KCI_URL, {k: v for k, v in p.items() if k != "key"})
        if cache and self.http.use_cache and ck.exists():
            raw = json.loads(ck.read_text(encoding="utf-8"))["body"]
        else:
            try:
                r = self.http.request(KCI_URL, p)
            except HttpError as e:
                raise KCIError(str(e)) from None
            if r is None:
                raise KCIError("KCI 응답 404")
            raw = r.content.decode("utf-8", "replace")
            self.calls += 1
        root = ET.fromstring(raw.encode("utf-8"))
        msg = _text(_find(root, "resultMsg"))
        rec = _findall(root, "record")
        if msg and not rec and "정상" not in msg:
            raise KCIError(f"KCI 응답 오류: {msg}")
        if cache and not ck.exists():
            ck.write_text(json.dumps({"url": KCI_URL, "params": {k: v for k, v in p.items() if k != "key"},
                                      "body": raw}, ensure_ascii=False), encoding="utf-8")
        return root

    def article_search(self, title: str | None = None, keyword: str | None = None, journal: str | None = None,
                       abstract: str | None = None, date_from: str | None = None, date_to: str | None = None,
                       display: int = 50, page: int = 1, cache: bool = True):
        params = {"apiCode": "articleSearch", "displayCount": min(display, 100), "page": page}
        for k, v in (("title", title), ("keyword", keyword), ("journal", journal), ("abstract", abstract),
                     ("dateFrom", date_from), ("dateTo", date_to)):
            if v:
                params[k] = v
        root = self._get_xml(params, cache)
        total = _text(_find(root, "total"))
        return (int(total) if total and total.isdigit() else None), _findall(root, "record"), root

    def reference_search(self, title: str, display: int = 50):
        root = self._get_xml({"apiCode": "referenceSearch", "title": title, "displayCount": min(display, 100)})
        return _findall(root, "record"), root


def kci_to_record(rec) -> dict:
    """KCI articleSearch record → papers 필드. 없는 필드는 None(지어내지 않음)."""
    j = _find(rec, "journalInfo")
    a = _find(rec, "articleInfo")
    titles = {}
    for t in _findall(a, "article-title") if a is not None else []:
        titles[t.get("lang") or "original"] = _text(t)
    abstracts = {}
    for t in _findall(a, "abstract") if a is not None else []:
        abstracts[t.get("lang") or "original"] = _text(t)
    authors = []
    for au in _findall(a, "author") if a is not None else []:
        raw = _text(au) or ""
        name, aff = raw, None
        if "(" in raw and raw.endswith(")"):
            name, aff = raw[:raw.index("(")].strip(), raw[raw.index("(") + 1:-1]
        authors.append({"name": name, "name_en": au.get("english"), "orcid": au.get("orc-id"), "affiliation": aff})
    cc = _find(a, "citation-count") if a is not None else None
    year = _text(_find(j, "pub-year")) if j is not None else None
    url = _text(_find(a, "url")) if a is not None else None
    open_yn = _text(_find(a, "orte-open-yn")) if a is not None else None
    orig = titles.get("original")
    en = titles.get("english") or titles.get("foreign")
    ko = orig if orig and any("\uac00" <= ch <= "\ud7a3" for ch in orig) else None

    def toint(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return {
        "kci_id": a.get("article-id") if a is not None else None,
        "doi": norm_doi(_text(_find(a, "doi"))) if a is not None else None,
        "uci": _text(_find(a, "uci")) if a is not None else None,
        "title": orig or en, "title_ko": ko, "title_en": en,
        "year": toint(year), "authors": authors,
        "venue": _text(_find(j, "journal-name")) if j is not None else None,
        "venue_source_id": f"KCI:{_text(_find(j, 'journal-name'))}" if j is not None and _text(_find(j, "journal-name")) else None,
        "venue_type": "journal", "work_type": "article",
        "language": "ko" if ko else None,
        "abstract": abstracts.get("original") or abstracts.get("english"),
        "abstract_source": "kci" if (abstracts.get("original") or abstracts.get("english")) else None,
        "kci_citation_count": toint(cc.get("kci")) if cc is not None else None,
        "wos_citation_count": toint(cc.get("wos")) if cc is not None else None,
        # KCI url은 상세(랜딩) 페이지 — PDF 직접 링크로 취급하지 않음(F05에서 REQUEST_USER)
        "oa": {"is_oa": open_yn == "Y", "oa_status": f"KCI orte-open-yn={open_yn}", "oa_url": url,
               "best": None, "locations": []},
        "db_origin": "KCI",
        "categories": _text(_find(a, "article-categories")) if a is not None else None,
        "publisher": _text(_find(j, "publisher-name")) if j is not None else None,
    }
