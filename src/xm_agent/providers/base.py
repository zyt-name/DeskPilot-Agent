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
    ) -> Action:
        ...
