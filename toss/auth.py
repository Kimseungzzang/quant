import json
import logging
import threading
import time
from datetime import datetime, timedelta

import requests

from .constants import TOSS_REST_URL, TOSS_TOKEN_DEFAULT_EXPIRES_IN, AuthPath, AccountPath

logger = logging.getLogger(__name__)


class TossAuth:
    """
    토스증권 OAuth2 Client Credentials 인증.
    - client당 유효 access token은 1개뿐이며, 재발급 시 이전 토큰은 즉시 무효화된다.
      → 만료 전까지는 캐시를 최대한 재사용하고, 불필요한 재발급을 피한다.
    - refresh token은 제공되지 않는다. 만료되면 동일 엔드포인트로 재발급.
    - accountSeq는 계좌 관련 API 전부에 X-Tossinvest-Account 헤더로 필요하므로
      토큰과 함께 캐싱해둔다.
    """

    def __init__(self, config: dict, redis_client=None):
        self.config = config
        toss_cfg = config.get("toss", {})
        self.client_id = toss_cfg.get("client_id", "")
        self.client_secret = toss_cfg.get("client_secret", "")
        self.base_url = toss_cfg.get("rest_url", TOSS_REST_URL)

        self._redis = redis_client
        self._redis_token_key = "toss:token"
        self._redis_account_key = "toss:account_seq"

        self._access_token: str | None = None
        self._token_expired_at: datetime | None = None
        self._account_seq: int | None = None
        self._token_lock = threading.Lock()

    def get_access_token(self) -> str:
        with self._token_lock:
            if self._is_token_valid():
                return self._access_token

            cached = self._load_token_cache()
            if cached:
                self._access_token = cached["access_token"]
                self._token_expired_at = datetime.fromisoformat(cached["expired_at"])
                if self._is_token_valid():
                    logger.debug("캐시에서 토스 토큰 로드")
                    return self._access_token

            return self._issue_token()

    def get_account_seq(self) -> int:
        if self._account_seq is not None:
            return self._account_seq

        cached = self._load_account_cache()
        if cached is not None:
            self._account_seq = cached
            return self._account_seq

        return self._fetch_account_seq()

    def invalidate_token(self) -> None:
        """다른 프로세스의 재발급 등으로 현재 토큰이 무효화됐을 때 강제로 캐시를 비운다.
        다음 get_access_token() 호출 시 새로 발급받는다."""
        with self._token_lock:
            self._access_token = None
            self._token_expired_at = None
            if self._redis:
                try:
                    self._redis.delete(self._redis_token_key)
                except Exception:
                    pass

    def get_headers(self, need_account: bool = False, **extra) -> dict:
        token = self.get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
        }
        if need_account:
            headers["X-Tossinvest-Account"] = str(self.get_account_seq())
        headers.update(extra)
        return headers

    def _is_token_valid(self) -> bool:
        if not self._access_token or not self._token_expired_at:
            return False
        # 만료 60초 전부터는 재발급 대상으로 간주 (경계에서 401 나는 것 방지)
        return datetime.now() < (self._token_expired_at - timedelta(seconds=60))

    def _issue_token(self) -> str:
        url = f"{self.base_url}{AuthPath.TOKEN}"
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        for attempt in range(3):
            resp = requests.post(
                url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
            if resp.status_code == 429 and attempt < 2:
                wait = float(resp.headers.get("Retry-After", "1")) + attempt
                logger.warning("토스 토큰 발급 rate limit — %.1f초 후 재시도 (%d/3)", wait, attempt + 1)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break

        body = resp.json()
        self._access_token = body["access_token"]
        expires_in = int(body.get("expires_in") or TOSS_TOKEN_DEFAULT_EXPIRES_IN)
        self._token_expired_at = datetime.now() + timedelta(seconds=expires_in)

        self._save_token_cache()
        logger.info("토스 신규 액세스 토큰 발급 완료, 만료: %s", self._token_expired_at.isoformat())
        return self._access_token

    def _fetch_account_seq(self) -> int:
        # ACCOUNT 그룹은 초당 1회 제한 — TossRestClient의 스로틀을 거치지 않는 별도 경로라
        # 직후에 /accounts를 또 호출하는 코드(예: 스모크테스트)와 겹치면 429가 날 수 있다.
        token = self.get_access_token()
        url = f"{self.base_url}{AccountPath.ACCOUNTS}"
        resp = None
        for attempt in range(3):
            resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
            if resp.status_code == 429 and attempt < 2:
                wait = float(resp.headers.get("Retry-After", "1.5")) + attempt
                logger.warning("토스 계좌 조회 rate limit — %.1f초 후 재시도 (%d/3)", wait, attempt + 1)
                time.sleep(wait)
                continue
            break
        resp.raise_for_status()
        accounts = resp.json().get("result") or []
        if not accounts:
            raise RuntimeError("토스 계좌 목록이 비어 있음 — WTS에서 종합매매 계좌 개설 여부 확인 필요")
        # 종합매매(BROKERAGE) 계좌 우선 선택. 여러 개면 첫 번째.
        brokerage = next((a for a in accounts if a.get("accountType") == "BROKERAGE"), accounts[0])
        self._account_seq = int(brokerage["accountSeq"])
        self._save_account_cache()
        logger.info("토스 계좌 조회 완료: accountNo=%s accountSeq=%s",
                    brokerage.get("accountNo"), self._account_seq)
        return self._account_seq

    def _load_token_cache(self) -> dict | None:
        if not self._redis:
            return None
        try:
            raw = self._redis.get(self._redis_token_key)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def _save_token_cache(self):
        if not self._redis:
            return
        ttl = int((self._token_expired_at - datetime.now()).total_seconds())
        if ttl <= 0:
            return
        try:
            self._redis.setex(
                self._redis_token_key,
                ttl,
                json.dumps({"access_token": self._access_token, "expired_at": self._token_expired_at.isoformat()}),
            )
        except Exception as e:
            logger.warning("Redis 토스 토큰 저장 실패: %s", e)

    def _load_account_cache(self) -> int | None:
        if not self._redis:
            return None
        try:
            raw = self._redis.get(self._redis_account_key)
            return int(raw) if raw else None
        except Exception:
            return None

    def _save_account_cache(self):
        if not self._redis:
            return
        try:
            # accountSeq는 사실상 불변이므로 넉넉히 30일 TTL
            self._redis.setex(self._redis_account_key, 30 * 24 * 3600, str(self._account_seq))
        except Exception as e:
            logger.warning("Redis 토스 accountSeq 저장 실패: %s", e)
