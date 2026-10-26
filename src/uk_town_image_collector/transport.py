"""Bounded HTTP fetcher for Wikimedia HTML and MediaWiki Action API calls.

Operational rules (Brief section 9):
- One request in flight at a time, >= 1.0 s delay between uncached requests.
- 20 s timeout; at most two retries (exponential backoff) for network errors and 5xx.
- Descriptive User-Agent with operator-configured HTTPS contact URL; gzip enabled.
- Stop the batch immediately on HTTP 401/403/429, Retry-After, CAPTCHA/challenge,
  robots disallow (for canonical HTML routes), or exhausted request budget.
- Cache 200 responses as compressed payloads with SHA-256 verification.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import requests

from .models import FetchResponse


ALLOWED_HOSTS = {"en.wikipedia.org", "commons.wikimedia.org"}


class StopRun(RuntimeError):
    def __init__(self, reason_code: str, message: str, response: FetchResponse | None = None):
        self.reason_code = reason_code
        self.response = response
        super().__init__(message)


class FetchError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class HtmlClient:
    def __init__(
        self,
        contact_url: str,
        cache_dir: Path,
        *,
        min_delay_seconds: float = 1.0,
        timeout_seconds: int = 20,
        max_response_bytes: int = 2_097_152,
        max_requests: int = 62,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.contact_url = _validate_contact_url(contact_url)
        self.user_agent = f"UKTownImageCollector/1.0 ({self.contact_url}) python-requests"
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_delay_seconds = min_delay_seconds
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.max_requests = max_requests
        self.session = session or requests.Session()
        self._sleep = sleep
        self.last_request_at = 0.0
        self.request_count = 0
        self.cache_hits = 0
        self.retry_count = 0
        self.request_log: list[dict[str, object]] = []

    def fetch_api_json(
        self,
        host: str,
        params: dict[str, str | int],
        *,
        use_cache: bool = True,
        purpose: str = "mediawiki_api",
    ) -> tuple[dict, FetchResponse]:
        if host not in ALLOWED_HOSTS:
            raise StopRun("DISALLOWED_HOST", f"Host not in allowlist: {host}")
        ordered = sorted((str(k), str(v)) for k, v in params.items())
        url = urlunsplit(("https", host, "/w/api.php", urlencode(ordered), ""))
        response = self.fetch(url, use_cache=use_cache, purpose=purpose)
        if response.status_code != 200:
            raise FetchError(f"Action API returned HTTP {response.status_code} for {url}", response.status_code)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FetchError(f"Invalid JSON from Action API at {url}: {exc}") from exc
        if isinstance(payload, dict) and "error" in payload:
            code = str((payload.get("error") or {}).get("code", "api_error"))
            if code in {"maxlag", "ratelimited", "readonly"}:
                raise StopRun("RATE_LIMITED", f"MediaWiki API {code} at {url}", response)
            raise FetchError(f"MediaWiki API error {code} at {url}")
        return payload if isinstance(payload, dict) else {}, response

    def fetch(
        self,
        url: str,
        *,
        route_guard: Callable[[str], bool] | None = None,
        use_cache: bool = True,
        purpose: str = "content",
    ) -> FetchResponse:
        is_api = purpose == "mediawiki_api"
        self._check_route(url, route_guard, purpose, is_api)
        cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        if use_cache:
            cached = self._read_cache(cache_key, url)
            if cached is not None:
                if purpose not in {"robots", "mediawiki_api"} and (route_guard is None or not route_guard(cached.final_url)):
                    raise StopRun("ROBOTS_DISALLOW", f"robots.txt does not allow cached final URL {cached.final_url}")
                self.cache_hits += 1
                self.request_log.append({"url": url, "purpose": purpose, "cache": True, "status": cached.status_code})
                return cached

        if self.request_count >= self.max_requests:
            raise StopRun("REQUEST_BUDGET_EXHAUSTED", f"Request cap {self.max_requests} reached before {url}")

        current = url
        chain: list[str] = []
        allowed_hosts = {urlsplit(url).hostname}
        accept = "application/json" if is_api else "text/html,application/xhtml+xml"
        for hop in range(6):
            self._check_route(current, route_guard, purpose, is_api, allowed_hosts=allowed_hosts)
            response: requests.Response | None = None
            last_exception: Exception | None = None
            for attempt in range(3):
                self._pace()
                self.request_count += 1
                if self.request_count > self.max_requests:
                    raise StopRun("REQUEST_BUDGET_EXHAUSTED", f"Request cap {self.max_requests} reached")
                try:
                    response = self.session.get(
                        current,
                        headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip", "Accept": accept},
                        timeout=self.timeout_seconds,
                        allow_redirects=False,
                        stream=True,
                    )
                    last_exception = None
                except requests.RequestException as exc:
                    last_exception = exc
                    if attempt < 2:
                        self.retry_count += 1
                        self._sleep(2**attempt)
                        continue
                    self.request_log.append({"url": current, "purpose": purpose, "cache": False, "error": type(exc).__name__})
                    raise FetchError(f"Network failure for {current}: {type(exc).__name__}: {exc}") from exc

                assert response is not None
                if response.headers.get("Retry-After"):
                    code = "RATE_LIMITED" if response.status_code == 429 else "RETRY_AFTER_STOP"
                    evidence = self._response_from_requests(url, current, response, chain)
                    response.close()
                    self.request_log.append({"url": current, "purpose": purpose, "cache": False, "status": evidence.status_code})
                    raise StopRun(code, f"Source returned Retry-After for {current}", evidence)
                if response.status_code in (401, 403, 429):
                    code = "RATE_LIMITED" if response.status_code == 429 else "BLOCKED"
                    evidence = self._response_from_requests(url, current, response, chain)
                    response.close()
                    self.request_log.append({"url": current, "purpose": purpose, "cache": False, "status": evidence.status_code})
                    raise StopRun(code, f"Source returned HTTP {evidence.status_code} for {current}", evidence)
                if response.status_code >= 500 and attempt < 2:
                    response.close()
                    self.retry_count += 1
                    self._sleep(2**attempt)
                    continue
                break

            if response is None:
                raise FetchError(f"No HTTP response for {current}") from last_exception
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                if is_api or not location or hop >= 5:
                    raise StopRun("UNEXPECTED_REDIRECT", f"Missing or excessive redirect from {current}")
                next_url = urljoin(current, location)
                next_parsed = urlsplit(next_url)
                if next_parsed.hostname not in allowed_hosts or next_parsed.scheme != "https" or not next_parsed.path.startswith("/wiki/"):
                    raise StopRun("UNEXPECTED_REDIRECT", f"Redirect left allowed canonical HTML route: {next_url}")
                chain.append(current)
                current = next_url
                continue

            body = self._read_bounded(response, current)
            status = response.status_code
            headers = {k.lower(): v for k, v in response.headers.items() if k.lower() in {"content-type", "last-modified", "etag"}}
            response.close()
            item = FetchResponse(url, current, status, body, hashlib.sha256(body).hexdigest(), utc_now(), headers, chain, False)
            if status == 200 and not is_api and _looks_like_challenge(body):
                raise StopRun("CHALLENGE", f"Challenge or CAPTCHA page detected for {current}", item)
            self.request_log.append({"url": current, "purpose": purpose, "cache": False, "status": status, "sha256": item.sha256})
            if status == 200:
                self._write_cache(cache_key, item)
            if status >= 500:
                raise FetchError(f"HTTP {status} from {current} after bounded retries", status)
            return item
        raise StopRun("UNEXPECTED_REDIRECT", f"Redirect limit reached for {url}")

    @staticmethod
    def _check_route(url: str, route_guard: Callable[[str], bool] | None, purpose: str, is_api: bool,
                     *, allowed_hosts: set[str | None] | None = None) -> None:
        parsed = urlsplit(url)
        hosts = allowed_hosts if allowed_hosts is not None else ALLOWED_HOSTS
        if parsed.scheme != "https" or parsed.hostname not in hosts or parsed.hostname not in ALLOWED_HOSTS:
            raise StopRun("UNEXPECTED_REDIRECT", f"Disallowed scheme or host: {url}")
        if purpose == "robots":
            if parsed.path != "/robots.txt":
                raise StopRun("UNEXPECTED_REDIRECT", f"Invalid robots path: {url}")
            return
        if is_api:
            if parsed.path != "/w/api.php":
                raise StopRun("UNEXPECTED_REDIRECT", f"Disallowed API endpoint: {url}")
            return
        if parsed.path.startswith(("/w/", "/api/")) or not parsed.path.startswith("/wiki/"):
            raise StopRun("ROBOTS_DISALLOW", f"Non-canonical HTML route is not allowed: {url}")
        if route_guard is None or not route_guard(url):
            raise StopRun("ROBOTS_DISALLOW", f"robots.txt does not allow {url}")

    def _pace(self) -> None:
        elapsed = time.monotonic() - self.last_request_at
        if self.last_request_at and elapsed < self.min_delay_seconds:
            self._sleep(self.min_delay_seconds - elapsed)
        self.last_request_at = time.monotonic()

    def _read_bounded(self, response: requests.Response, url: str) -> bytes:
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > self.max_response_bytes:
            response.close()
            raise FetchError(f"Response exceeded {self.max_response_bytes} bytes at {url}", response.status_code)
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=65_536):
            size += len(chunk)
            if size > self.max_response_bytes:
                response.close()
                raise FetchError(f"Response exceeded {self.max_response_bytes} bytes at {url}", response.status_code)
            chunks.append(chunk)
        return b"".join(chunks)

    def _response_from_requests(self, requested: str, current: str, response: requests.Response, chain: list[str]) -> FetchResponse:
        body = self._read_bounded(response, current)
        return FetchResponse(requested, current, response.status_code, body, hashlib.sha256(body).hexdigest(), utc_now(), {}, list(chain), False)

    def _cache_paths(self, key: str) -> tuple[Path, Path]:
        return self.cache_dir / f"{key}.html.gz", self.cache_dir / f"{key}.json"

    def _read_cache(self, key: str, requested_url: str) -> FetchResponse | None:
        body_path, meta_path = self._cache_paths(key)
        if not body_path.exists() or not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if time.time() - float(meta["saved_epoch"]) > 7 * 24 * 60 * 60:
                return None
            body = gzip.decompress(body_path.read_bytes())
            if hashlib.sha256(body).hexdigest() != meta["sha256"]:
                return None
            return FetchResponse(
                requested_url, meta["final_url"], int(meta["status_code"]), body, meta["sha256"],
                meta["retrieved_at_utc"], meta.get("headers", {}), meta.get("redirect_chain", []), True,
            )
        except (OSError, KeyError, ValueError, json.JSONDecodeError, gzip.BadGzipFile):
            return None

    def _write_cache(self, key: str, item: FetchResponse) -> None:
        body_path, meta_path = self._cache_paths(key)
        tmp_body = body_path.with_suffix(".tmp")
        tmp_meta = meta_path.with_suffix(".tmp")
        tmp_body.write_bytes(gzip.compress(item.body))
        tmp_meta.write_text(json.dumps({
            "final_url": item.final_url, "status_code": item.status_code, "sha256": item.sha256,
            "retrieved_at_utc": item.retrieved_at_utc, "headers": item.headers,
            "redirect_chain": item.redirect_chain, "saved_epoch": time.time(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_body.replace(body_path)
        tmp_meta.replace(meta_path)


def _validate_contact_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("WIKI_CONTACT_URL must be a public HTTPS profile or project URL without credentials/query/fragment")
    return value.strip().rstrip("/")


def _looks_like_challenge(body: bytes) -> bool:
    sample = body[:500_000].decode("utf-8", errors="ignore").casefold()
    return bool(re.search(r"id=[\"'](?:captcha|captcha-container)[\"']|class=[\"'][^\"']*captcha|just a moment\.\.\.|security check|robot check", sample))
