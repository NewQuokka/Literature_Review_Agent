"""경로·환경설정·HTTP(캐시 포함)."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import requests

USER_AGENT = "adaptive-literature-review/0.1 (research support tool; python-requests)"
QUOTE_MAX_WORDS = 40          # 직접 인용 상한(저작권상 짧은 발췌)
CORE_TARGET = 20              # 합의된 Core 목표치(의무 아님)


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

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None,
                 min_interval: float = 0.0, cache: bool = True, retries: int = 3):
        params = dict(params or {})
        ck = self._key(url, params)
        if self.use_cache and cache and ck.exists():
            return json.loads(ck.read_text(encoding="utf-8"))["body"]
        host = url.split("/")[2]
        for attempt in range(retries):
            wait = min_interval - (time.time() - self._last_call.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            self._last_call[host] = time.time()
            r = self.s.get(url, params=params, headers=headers or {}, timeout=60)
            self.last_headers = dict(r.headers)
            if r.status_code == 429 or r.status_code >= 500:
                ra = r.headers.get("Retry-After")
                time.sleep(float(ra) if ra and ra.isdigit() and int(ra) < 60 else 2 * (attempt + 1))
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            body = r.json()
            if cache:
                ck.write_text(json.dumps({"url": url, "params": {k: v for k, v in params.items() if k != "api_key"},
                                          "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "body": body},
                                         ensure_ascii=False), encoding="utf-8")
            return body
        raise RuntimeError(f"HTTP 재시도 초과: {url} (마지막 상태 {r.status_code}: {r.text[:200]})")
