"""경로·환경설정·HTTP(캐시 포함)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

import requests

USER_AGENT = "adaptive-literature-review/0.1 (research support tool; python-requests)"
QUOTE_MAX_WORDS = 40          # 직접 인용 상한(저작권상 짧은 발췌)
CORE_TARGET = 20              # 합의된 Core 목표치(의무 아님)
EVIDENCE_TYPES = ("concept", "theory", "method", "finding", "statistic", "result", "limitation", "definition")
MIN_FULLTEXT_RATE = 0.5       # Core+Extended 원문 확보율이 이보다 낮으면 V06 INSUFFICIENT(#11)
MIN_VERIFIED_QUOTE_RATE = 0.5  # 직접 인용 중 원문 대조 확인 비율이 이보다 낮으면 V07 INSUFFICIENT
INTEGRITY_MAX_AGE = 24 * 3600  # 정정·철회 조회 캐시 유효기간(초). 철회 공지는 사후에 나오므로 짧게 둔다
INTEGRITY_STALE_DAYS = 30     # 마지막 정정·철회 점검이 이보다 오래되면 V14 WARN


class Paths:
    def __init__(self, root: str | os.PathLike | None = None):
        self.root = Path(root or os.environ.get("LRA_ROOT") or os.getcwd()).resolve()
        self.raw = self.root / "raw"
        self.state = self.root / "state"
        self.db = self.state / "literature.db"
        self.profile = self.state / "research_profile.json"
        self.cache = self.state / "cache"
        self.text = self.state / "text"
        self.quarantine = self.state / "quarantine"
        self.library = self.root / "library"
        self.out = self.root / "out"
        self.config = self.state / "config.json"
        self.raw_manifest = self.state / "raw_manifest.json"

    def ensure(self):
        for p in (self.raw, self.state, self.cache, self.text, self.quarantine,
                  self.library / "Core", self.library / "Extended", self.out):
            p.mkdir(parents=True, exist_ok=True)


_SECRET_RE = re.compile(r"((?:api_key|key|mailto)=)[^&\s'\"]+", re.I)


def redact(s: str) -> str:
    """URL·오류 메시지의 API 키·이메일을 가린다(대화 기록·로그에 남지 않게)."""
    return _SECRET_RE.sub(r"\1***", s)


class HttpError(RuntimeError):
    """비밀값을 가린 HTTP 오류. 원래 예외(키가 든 URL 포함)는 연결하지 않는다."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(redact(msg))
        self.status = status


def load_config(paths: Paths) -> dict:
    cfg = {}
    if paths.config.exists():
        cfg = json.loads(paths.config.read_text(encoding="utf-8"))
    # 환경변수가 우선
    for env, key in (("OPENALEX_API_KEY", "openalex_api_key"),
                     ("S2_API_KEY", "s2_api_key"),
                     ("KCI_API_KEY", "kci_api_key"),
                     ("LRA_MAILTO", "mailto")):
        if os.environ.get(env):
            cfg[key] = os.environ[env]
    return cfg


class Http:
    """GET + JSON 응답 디스크 캐시. 비용이 드는 목록 조회를 반복하지 않기 위함."""

    def __init__(self, paths: Paths, use_cache: bool = True, verbose: bool = False):
        self.paths = paths
        self.use_cache = use_cache
        self.verbose = verbose
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.last_headers: dict = {}
        self._last_call: dict[str, float] = {}

    def _key(self, url: str, params: dict) -> Path:
        # API 키는 캐시 키에서 제외
        p = {k: v for k, v in sorted(params.items()) if k not in ("api_key",)}
        h = hashlib.sha1((url + json.dumps(p, ensure_ascii=False)).encode()).hexdigest()
        return self.paths.cache / f"{h}.json"

    def request(self, url: str, params: dict | None = None, headers: dict | None = None,
                min_interval: float = 0.0, retries: int = 3):
        """GET(재시도 포함). 404면 None. 실패 시 비밀값을 가린 HttpError.
        시간 초과·연결 오류도 429·5xx처럼 재시도한다."""
        host = url.split("/")[2]
        last = ""
        for attempt in range(retries):
            wait = min_interval - (time.time() - self._last_call.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self._last_call[host] = time.time()
            try:
                r = self.s.get(url, params=params, headers=headers or {}, timeout=60)
            except (requests.Timeout, requests.ConnectionError) as e:
                last = f"{type(e).__name__}: {e}"
                time.sleep(2 * (attempt + 1))
                continue
            self.last_headers = r.headers
            if r.status_code == 429 or r.status_code >= 500:
                last = f"상태 {r.status_code}: {r.text[:200]}"
                ra = r.headers.get("Retry-After")
                time.sleep(float(ra) if ra and ra.isdigit() and int(ra) < 60 else 2 * (attempt + 1))
                continue
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                raise HttpError(f"HTTP {r.status_code}: {url} — {r.text[:200]}", r.status_code) from None
            return r
        raise HttpError(f"HTTP 재시도 초과: {url} (마지막 {last})") from None

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None,
                 min_interval: float = 0.0, cache: bool = True, retries: int = 3,
                 max_age: float | None = None):
        """max_age(초): 캐시가 이보다 오래됐으면 다시 받는다. None이면 만료 없음."""
        params = dict(params or {})
        ck = self._key(url, params)
        if (self.use_cache and cache and ck.exists()
                and (max_age is None or time.time() - ck.stat().st_mtime <= max_age)):
            return json.loads(ck.read_text(encoding="utf-8"))["body"]
        r = self.request(url, params, headers, min_interval, retries)
        if r is None:
            return None
        body = r.json()
        if cache:
            ck.write_text(json.dumps({"url": url, "params": {k: v for k, v in params.items() if k != "api_key"},
                                      "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "body": body},
                                     ensure_ascii=False), encoding="utf-8")
        return body
