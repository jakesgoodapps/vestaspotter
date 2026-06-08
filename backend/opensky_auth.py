"""OpenSky Network auth — OAuth2 with token cache, legacy Basic fallback.

Lifted from PlaneSpotter unchanged.
"""
import asyncio
import time
from typing import Optional

import aiohttp


_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/"
    "opensky-network/protocol/openid-connect/token"
)


class OpenSkyAuth:
    def __init__(
        self,
        client_id: str = "",
        client_secret: str = "",
        username: str = "",
        password: str = "",
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.username = username
        self.password = password
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._refresh_lock = asyncio.Lock()

    @property
    def is_oauth(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def is_legacy_basic(self) -> bool:
        return bool(self.username and self.password) and not self.is_oauth

    @property
    def basic(self) -> Optional[aiohttp.BasicAuth]:
        if self.is_legacy_basic:
            return aiohttp.BasicAuth(self.username, self.password)
        return None

    async def headers(self, session: aiohttp.ClientSession) -> dict[str, str]:
        if self.is_oauth:
            token = await self._token_fresh(session)
            return {"Authorization": f"Bearer {token}"} if token else {}
        return {}

    async def _token_fresh(self, session: aiohttp.ClientSession) -> Optional[str]:
        now = time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token
        async with self._refresh_lock:
            now = time.time()
            if self._token and now < self._token_expires_at - 60:
                return self._token
            try:
                async with session.post(
                    _TOKEN_URL,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        print(f"OpenSky token fetch failed: {resp.status}")
                        return None
                    data = await resp.json()
            except Exception as e:
                print(f"OpenSky token fetch error: {e}")
                return None
            self._token = data.get("access_token")
            self._token_expires_at = time.time() + (data.get("expires_in") or 1800)
            return self._token
