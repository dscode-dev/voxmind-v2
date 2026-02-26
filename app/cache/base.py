from __future__ import annotations

from abc import ABC, abstractmethod


class Cache(ABC):
    @abstractmethod
    def get(self, key: str) -> str | None: ...

    @abstractmethod
    def set(self, key: str, value: str, ttl_seconds: int) -> None: ...


class NoopCache(Cache):
    def get(self, key: str) -> str | None:
        return None

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        return None
