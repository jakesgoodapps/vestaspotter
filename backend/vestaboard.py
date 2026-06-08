import logging
import os
from abc import ABC, abstractmethod

import httpx

from .formatter import render_ascii

VESTABOARD_RW_URL = "https://rw.vestaboard.com/"

logger = logging.getLogger(__name__)


class VestaboardClient(ABC):
    @abstractmethod
    async def push(self, matrix: list[list[int]]) -> None: ...


class CloudClient(VestaboardClient):
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def push(self, matrix: list[list[int]]) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                VESTABOARD_RW_URL,
                headers={"X-Vestaboard-Read-Write-Key": self.api_key},
                json=matrix,
            )
        # 409 Conflict means "this message equals the current board state" —
        # Vestaboard skips the flap. Treat as success.
        if resp.status_code == 409:
            logger.info("vestaboard cloud push no-op (409, board already matches)")
            return
        resp.raise_for_status()
        logger.info("vestaboard cloud push ok status=%s", resp.status_code)


class DryRunClient(VestaboardClient):
    async def push(self, matrix: list[list[int]]) -> None:
        preview = render_ascii(matrix)
        logger.info("DRY RUN — would push:\n%s", preview)


def make_client() -> VestaboardClient:
    dry_run = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")
    if dry_run:
        return DryRunClient()
    api_key = os.getenv("VESTABOARD_API_KEY")
    if not api_key:
        raise RuntimeError("VESTABOARD_API_KEY not set and DRY_RUN is false")
    return CloudClient(api_key)
