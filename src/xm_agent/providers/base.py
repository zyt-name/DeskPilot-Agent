from __future__ import annotations

from typing import Protocol

from ..types import Action, Observation, StepRecord


class Provider(Protocol):
    name: str

    def decide(
        self,
        goal: str,
        observation: Observation,
        history: list[StepRecord],
        remaining_steps: int,
        *,
        instructions: list[str] | None = None,
        on_delta=None,
        context: str = "",
    ) -> Action:
        ...

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 800,
        on_delta=None,
    ) -> str:
        ...
