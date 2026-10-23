"""Shared HTTP client: disk cache, conditional GET, polite per-host floor, retries."""

from __future__ import annotations

import gzip
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15"
)
POLITE_FLOOR_SECONDS = 1.0
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 2.0  # seconds
DEFAULT_TIMEOUT = 30.0  # seconds


def canonical_url(url: str) -> str:
    """Strip volatile query params (e.g. ?t=...) so cache key is stable."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    keep = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() != "t"]
    return urlunsplit(parts._replace(query=urlencode(keep)))


def url_sha1(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()


@dataclass
class CacheEntry:
    url: str
    sha1: str
    fetched_at: str
    status: int
    etag: Optional[str]
    last_modified: Optional[str]


class HttpClient:
    def __init__(self, storage_root: Path, user_agent: str = DEFAULT_UA,
                 polite_floor: float = POLITE_FLOOR_SECONDS):
        self.storage_root = Path(storage_root)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.polite_floor = polite_floor
        self._last_request_at: dict[str, float] = {}

    def _raw_dir(self, source: str) -> Path:
        d = self.storage_root / "raw" / source
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _body_path(self, source: str, sha1: str) -> Path:
        return self._raw_dir(source) / f"{sha1}.html.gz"

    def _index_path(self, source: str) -> Path:
        return self._raw_dir(source) / "index.jsonl"

    def _read_index(self, source: str) -> dict[str, dict]:
        """Returns {sha1: latest entry dict}. JSONL is append-only; last write wins."""
        idx: dict[str, dict] = {}
        path = self._index_path(source)
        if not path.exists():
            return idx
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                idx[rec["sha1"]] = rec
        return idx

    def _append_index(self, source: str, entry: dict) -> None:
        with self._index_path(source).open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _read_body(self, source: str, sha1: str) -> Optional[bytes]:
        p = self._body_path(source, sha1)
        if not p.exists():
            return None
        with gzip.open(p, "rb") as f:
            return f.read()

    def _write_body(self, source: str, sha1: str, body: bytes) -> None:
        p = self._body_path(source, sha1)
        with gzip.open(p, "wb") as f:
            f.write(body)

    def _polite_wait(self, host: str) -> None:
        now = time.monotonic()
        last = self._last_request_at.get(host, 0.0)
        wait = self.polite_floor - (now - last)
        if wait > 0:
            time.sleep(wait)

    def _record_request(self, host: str) -> None:
        self._last_request_at[host] = time.monotonic()

    def get(self, url: str, source: str, referer: Optional[str] = None,
            force: bool = False,
            extra_headers: Optional[dict] = None) -> bytes:
        """Return body bytes. Cache hits skip network entirely."""
        sha1 = url_sha1(url)
        cached = self._read_body(source, sha1)
        idx = self._read_index(source)
        prev = idx.get(sha1)

        if cached is not None and not force:
            return cached

        host = urlsplit(url).netloc
        headers = {}
        if referer:
            headers["Referer"] = referer
        if prev:
            if prev.get("etag"):
                headers["If-None-Match"] = prev["etag"]
            if prev.get("last_modified"):
                headers["If-Modified-Since"] = prev["last_modified"]
        if extra_headers:
            headers.update(extra_headers)

        last_exc: Optional[Exception] = None
        for attempt in range(DEFAULT_RETRIES):
            self._polite_wait(host)
            try:
                self._record_request(host)
                resp = self.session.get(url, headers=headers,
                                        timeout=DEFAULT_TIMEOUT,
                                        allow_redirects=True)
                if resp.status_code == 304 and cached is not None:
                    self._append_index(source, {
                        "url": canonical_url(url),
                        "sha1": sha1,
                        "fetched_at": _now_iso(),
                        "status": 304,
                        "etag": prev.get("etag") if prev else None,
                        "last_modified": prev.get("last_modified") if prev else None,
                    })
                    return cached
                resp.raise_for_status()
                body = resp.content
                self._write_body(source, sha1, body)
                self._append_index(source, {
                    "url": canonical_url(url),
                    "sha1": sha1,
                    "fetched_at": _now_iso(),
                    "status": resp.status_code,
                    "etag": resp.headers.get("ETag"),
                    "last_modified": resp.headers.get("Last-Modified"),
                })
                return body
            except (requests.ConnectionError, requests.Timeout) as e:
                last_exc = e
                if attempt < DEFAULT_RETRIES - 1:
                    time.sleep(DEFAULT_BACKOFF * (2 ** attempt))
                continue
            except requests.HTTPError as e:
                # 4xx/5xx — don't retry blindly; bubble up
                raise
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("unreachable")

    def iter_cached(self, source: str):
        """Yield (url, sha1, body, fetched_at) for every entry currently cached."""
        idx = self._read_index(source)
        for sha1, rec in idx.items():
            body = self._read_body(source, sha1)
            if body is None:
                continue
            yield rec["url"], sha1, body, rec.get("fetched_at", "")


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
