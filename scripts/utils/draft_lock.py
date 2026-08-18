import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Optional


class DraftLockManager:
    _instances = {}

    def __new__(cls, lock_root: Optional[str] = None, poll_interval: float = 0.05):
        resolved_root = os.path.abspath(
            lock_root or os.path.join(tempfile.gettempdir(), "jianying-draft-locks")
        )
        key = (resolved_root, float(poll_interval))
        if key not in cls._instances:
            instance = super().__new__(cls)
            instance._lock_root = resolved_root
            instance._poll_interval = float(poll_interval)
            instance._manager_lock = asyncio.Lock()
            os.makedirs(instance._lock_root, exist_ok=True)
            cls._instances[key] = instance
        return cls._instances[key]

    def _lock_dir(self, draft_id: str) -> str:
        digest = hashlib.sha1(str(draft_id).encode("utf-8")).hexdigest()
        return os.path.join(self._lock_root, digest)

    def _metadata_path(self, draft_id: str) -> str:
        return os.path.join(self._lock_dir(draft_id), "owner.json")

    async def acquire_lock(self, draft_id: str, timeout: Optional[float] = None) -> bool:
        lock_dir = self._lock_dir(draft_id)
        start = time.monotonic()
        while True:
            try:
                os.mkdir(lock_dir)
                metadata = {
                    "draft_id": str(draft_id),
                    "pid": os.getpid(),
                    "acquired_at": time.time(),
                }
                with open(self._metadata_path(draft_id), "w", encoding="utf-8") as f:
                    json.dump(metadata, f)
                return True
            except FileExistsError:
                if timeout is not None and (time.monotonic() - start) >= timeout:
                    raise asyncio.TimeoutError(f"Timed out waiting for lock: {draft_id}")
                await asyncio.sleep(self._poll_interval)

    async def release_lock(self, draft_id: str) -> None:
        lock_dir = self._lock_dir(draft_id)
        if not os.path.isdir(lock_dir):
            raise KeyError(f"No lock found for draft_id: {draft_id}")
        metadata_path = self._metadata_path(draft_id)
        if os.path.exists(metadata_path):
            try:
                os.remove(metadata_path)
            except OSError:
                pass
        shutil.rmtree(lock_dir, ignore_errors=True)

    def is_locked(self, draft_id: str) -> bool:
        return os.path.isdir(self._lock_dir(draft_id))

    @asynccontextmanager
    async def hold(self, draft_id: str, timeout: Optional[float] = None):
        await self.acquire_lock(draft_id, timeout=timeout)
        try:
            yield
        finally:
            await self.release_lock(draft_id)
