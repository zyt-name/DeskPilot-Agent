from __future__ import annotations

import re

from ..types import Action, Observation, StepRecord


class ScriptedProvider:
    """Replays a fixed action list; used by the demo and unit tests."""

    name = "scripted"

    def __init__(self, actions: list[Action]) -> None:
        self._actions = list(actions)
        self.index = 0

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
        if self.index >= len(self._actions):
            action = Action("task.finish", {"summary": "script complete"})
        else:
            action = self._actions[self.index]
            self.index += 1
        if on_delta is not None:
            on_delta("text", f"{action.capability} {action.args}")
        return action

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 800,
        on_delta=None,
    ) -> str:
        raise NotImplementedError("scripted provider has no completion model")


class HeuristicMockProvider:
    """Small rule set so `run` can be exercised without a cloud model."""

    name = "heuristic-mock"

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
        action = self._decide(goal, observation, instructions)
        if on_delta is not None:
            if action.rationale:
                on_delta("thinking", action.rationale + "\n")
            on_delta("text", f"{action.capability} {action.args}\n")
        return action

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 800,
        on_delta=None,
    ) -> str:
        return (
            '{"verdict":"ask_user","risk":"medium",'
            '"categories":["unknown"],'
            '"purpose":"本地模拟模型不能审计","impact":"需要用户确认",'
            '"steps":["等待用户检查"],"safer_alternative":""}'
        )

    def _decide(
        self,
        goal: str,
        observation: Observation,
        instructions: list[str] | None,
    ) -> Action:
        if instructions:
            goal = goal + "\n" + "\n".join(instructions)
        summary = observation.summary
        if "saved=True" in summary:
            return Action("task.finish", {"summary": "mock task complete"})

        target = self._target_text(goal)
        if target and f"text='{target}'" not in summary:
            return Action(
                "desktop.act",
                {"action": "type", "text": target},
                "type the requested text",
                expect=target,
            )

        if "保存" in goal and "saved=False" in summary:
            return Action(
                "desktop.act",
                {"action": "click", "element_id": "e2"},
                "click the Save button",
                expect="saved",
            )

        return Action("task.finish", {"summary": "mock task complete"})

    @staticmethod
    def _target_text(goal: str) -> str | None:
        quoted = re.findall(r"['\"]([^'\"]+)['\"]", goal)
        if quoted:
            return quoted[0]
        words = re.findall(r"[A-Za-z0-9_.-]{2,}", goal)
        return words[0] if words else None
