import logging
import threading
import time
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .constants import (
    RATE_LIMIT_PER_SEC, PEAK_RATE_LIMIT_PER_SEC, PEAK_HOUR_START, PEAK_HOUR_END,
)

logger = logging.getLogger(__name__)


class TossAPIError(Exception):
    def __init__(self, message: str, code: str = "", status: int = 0, request_id: str = ""):
        super().__init__(message)
        self.code = code
        self.status = status
        self.request_id = request_id


def _is_peak_hour() -> bool:
    now = datetime.now().time()
    start = now.replace(hour=PEAK_HOUR_START[0], minute=PEAK_HOUR_START[1], second=0, microsecond=0)
    end = now.replace(hour=PEAK_HOUR_END[0], minute=PEAK_HOUR_END[1], second=0, microsecond=0)
    return start <= now <= end


class TossRestClient:
    """
    토스증권 REST 클라이언트.
    - 그룹별 rate limit을 각각 독립적으로 스로틀링한다 (KIS처럼 전역 단일 스로틀이 아님).
    - 429 응답은 Retry-After 헤더 기준으로 대기 후 재시도한다.
    - 에러는 envelope({"error": {"code","message","requestId"}}) 파싱해 TossAPIError로 변환한다.
    """

    def __init__(self, auth):
        self.auth = auth
        self.base_url = auth.base_url
        self.session = self._build_session()
        self._last_request_time: dict[str, float] = {}
        self._throttle_lock = threading.Lock()

    def get(self, path: str, group: str, params: dict | None = None,
            need_account: bool = False, timeout: int = 10) -> dict:
        url = f"{self.base_url}{path}"
        resp = self._send_with_retry(
            lambda: self.session.get(
                url, headers=self.auth.get_headers(need_account=need_account),
                params=params, timeout=timeout,
            ),
            group=group, retry_limit=2,
        )
        return self._handle(resp)

    def post(self, path: str, group: str, json_body: dict | None = None,
              need_account: bool = False, timeout: int = 10) -> dict:
        url = f"{self.base_url}{path}"
        headers = self.auth.get_headers(need_account=need_account, **{"Content-Type": "application/json"})
        resp = self._send_with_retry(
            lambda: self.session.post(url, headers=headers, json=json_body or {}, timeout=timeout),
            group=group, retry_limit=1,
        )
        return self._handle(resp)

    def delete(self, path: str, group: str, need_account: bool = False, timeout: int = 10) -> dict:
        url = f"{self.base_url}{path}"
        resp = self._send_with_retry(
            lambda: self.session.delete(
                url, headers=self.auth.get_headers(need_account=need_account), timeout=timeout,
            ),
            group=group, retry_limit=1,
        )
        return self._handle(resp)

    # ── 내부 ────────────────────────────────────────────────────────────

    def _throttle(self, group: str):
        limits = PEAK_RATE_LIMIT_PER_SEC if _is_peak_hour() else {}
        limit_per_sec = limits.get(group) or RATE_LIMIT_PER_SEC.get(group, 5)
        min_interval = 1.0 / limit_per_sec
        with self._throttle_lock:
            last = self._last_request_time.get(group, 0.0)
            elapsed = time.monotonic() - last
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            self._last_request_time[group] = time.monotonic()

    def _send_with_retry(self, send, group: str, retry_limit: int) -> requests.Response:
        # 토큰은 client당 1개만 유효 — 다른 프로세스가 재발급하면 기존 토큰이 즉시
        # 무효화되어 401(invalid-token/expired-token)이 온다. 이 경우 재발급 후
        # retry_limit 예산과 무관하게 1회는 무조건 재시도한다.
        token_retried = False
        attempt = 0
        while True:
            self._throttle(group)
            resp = send()
            if resp.status_code == 429 and attempt < retry_limit:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else (2 ** attempt)
                logger.warning("토스 rate limit(%s) — %.1f초 후 재시도 (%d/%d)",
                                group, wait, attempt + 1, retry_limit)
                time.sleep(wait)
                attempt += 1
                continue
            if resp.status_code == 401 and not token_retried:
                code = self._extract_error_code(resp)
                if code in ("invalid-token", "expired-token"):
                    logger.warning("토스 토큰 무효화 감지(%s) — 재발급 후 재시도", code)
                    self.auth.invalidate_token()
                    token_retried = True
                    continue
            return resp

    @staticmethod
    def _extract_error_code(resp: requests.Response) -> str:
        try:
            return (resp.json().get("error") or {}).get("code", "")
        except Exception:
            return ""

    def _handle(self, resp: requests.Response) -> dict:
        try:
            data = resp.json() if resp.text else {}
        except ValueError:
            resp.raise_for_status()
            raise RuntimeError(f"토스 API 응답 파싱 실패: body={resp.text[:200]}")

        if not resp.ok:
            err = (data or {}).get("error") or {}
            message = err.get("message") or resp.text[:200] or f"HTTP {resp.status_code}"
            logger.error("토스 API 오류 [%s] %s: %s (requestId=%s)",
                          resp.status_code, err.get("code"), message, err.get("requestId"))
            raise TossAPIError(
                message, code=err.get("code", ""), status=resp.status_code,
                request_id=err.get("requestId", ""),
            )
        return data

    def _build_session(self) -> requests.Session:
        session = requests.Session()
        retry = Retry(total=2, backoff_factor=1, status_forcelist=[502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session
