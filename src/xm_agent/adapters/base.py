from __future__ import annotations

from typing import Any, Protocol

from ..types import Observation, Receipt, Verification


class Adapter(Protocol):
    name: str

    def observe(self, step: int, include_screenshot: bool = False) -> Observation:
        ...

    def execute(self, capability: str, args: dict[str, Any]) -> Receipt:
        ...

    def verify(self, expect: str | dict[str, Any] | None) -> Verification:
        ...

    def close(self) -> None:
        ...
