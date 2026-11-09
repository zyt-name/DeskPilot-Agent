from __future__ import annotations

import json

from xm_agent.contracts import CAPABILITIES
from xm_agent.guard import (
    AuditGate,
    TopLevelAuditor,
    detect_sensitivity,
    normalize_operation,
    rule_verdict,
)
from xm_agent.guard.auditor import fallback_verdict
from xm_agent.policy import Policy
from xm_agent.types import Action, Element, Observation, PolicyDecision


class StubAuditProvider:
    def __init__(
        self,
        verdict: str = "allow",
        error: str | None = None,
        scope: dict | None = None,
        categories: list[str] | None = None,
    ) -> None:
        self.verdict = verdict
        self.error = error
        self.scope = scope or {"inside_project": True, "cross_project": False}
        self.categories = categories or ["data_loss"]
        self.calls: list[dict[str, str]] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 800) -> str:
        self.calls.append({"system": system, "user": user})
        if self.error:
            raise RuntimeError(self.error)
        return json.dumps(
            {
                "verdict": self.verdict,
                "risk": "medium",
                "categories": self.categories,
                "scope": self.scope,
                "reversible": False,
                "purpose": "删除项目目录",
                "impact": "目标目录中的文件会丢失",
                "steps": ["定位目录", "递归删除"],
                "safer_alternative": "先备份再删除",
            },
            ensure_ascii=False,
        )


def _gate(
    tmp_path,
    *,
    mode: str = "ai",
    verdict: str = "allow",
    error: str | None = None,
    confirm: bool = True,
    confirm_calls: list[str] | None = None,
    on_decision=None,
    scope: dict | None = None,
    categories: list[str] | None = None,
) -> tuple[AuditGate, StubAuditProvider]:
    provider = StubAuditProvider(
        verdict=verdict,
        error=error,
        scope=scope,
        categories=categories,
    )
    auditor = TopLevelAuditor(provider, model="audit-model")
    calls = confirm_calls if confirm_calls is not None else []

    def confirm_callback(
        action: Action, prompt: str, mandatory: bool = False
    ) -> bool:
        calls.append(prompt)
        return confirm

    gate = AuditGate(
        mode=mode,
        auditor=auditor,
        confirm=confirm_callback,
        on_decision=on_decision,
        workspace_roots=[str(tmp_path)],
        project_root=str(tmp_path),
    )
    return gate, provider


def test_read_only_scroll_is_not_audited(tmp_path):
    gate, provider = _gate(tmp_path)
    decision = gate.review(
        Action("desktop.act", {"action": "scroll", "amount": 3}),
        PolicyDecision("allow", "allowed"),
    )
    assert decision.allowed is True
    assert decision.audited is False
    assert provider.calls == []


def test_policy_denial_is_not_sent_to_auditor(tmp_path):
    gate, provider = _gate(tmp_path)
    decision = gate.review(
        Action("terminal.exec", {"command": "rm -r data"}),
        PolicyDecision("deny", "command is not in the configured allowlist"),
    )
    assert decision.allowed is False
    assert decision.verdict.source == "policy"
    assert decision.audited is False
    assert provider.calls == []


def test_write_to_hard_block_path_is_denied_by_rule(tmp_path):
    gate, provider = _gate(tmp_path)
    decision = gate.review(
        Action(
            "file.transfer",
            {
                "direction": "download",
                "source": "https://example.com/a.txt",
                "destination": "C:/Windows/System32/evil.txt",
            },
        ),
        PolicyDecision("allow", "allowed"),
    )
    assert decision.allowed is False
    assert decision.verdict.source == "rule"
    assert decision.verdict.risk == "critical"
    assert decision.audited is True
    assert provider.calls == []


def test_ai_allow_executes_without_user_prompt(tmp_path):
    records: list[dict] = []
    gate, provider = _gate(
        tmp_path, verdict="allow", on_decision=records.append
    )
    decision = gate.review(
        Action("terminal.exec", {"command": "rm -r build"}),
        PolicyDecision("allow", "allowed"),
    )
    assert decision.allowed is True
    assert decision.user_confirmed is False
    assert decision.verdict.verdict == "allow"
    assert decision.verdict.source == "ai"
    assert len(provider.calls) == 1
    assert records and records[0]["operation"]["capability"] == "terminal.exec"


def test_ai_ask_user_prompt_contains_purpose_and_impact(tmp_path):
    prompts: list[str] = []
    gate, _ = _gate(
        tmp_path, verdict="ask_user", confirm=True, confirm_calls=prompts
    )
    decision = gate.review(
        Action("terminal.exec", {"command": "rm -r build"}),
        PolicyDecision("allow", "allowed"),
    )
    assert decision.allowed is True
    assert decision.user_confirmed is True
    assert prompts
    for label in ("操作：", "目的：", "流程：", "可能影响：", "可逆性："):
        assert label in prompts[0]


def test_user_denial_blocks_the_operation(tmp_path):
    gate, _ = _gate(tmp_path, verdict="ask_user", confirm=False)
    decision = gate.review(
        Action("terminal.exec", {"command": "rm -r build"}),
        PolicyDecision("allow", "allowed"),
    )
    assert decision.allowed is False
    assert "用户拒绝" in decision.reason


def test_auditor_failure_falls_back_to_user_confirmation(tmp_path):
    gate, provider = _gate(tmp_path, error="network down", confirm=False)
    decision = gate.review(
        Action("terminal.exec", {"command": "rm -r build"}),
        PolicyDecision("allow", "allowed"),
    )
    assert decision.allowed is False
    assert decision.verdict.source == "fallback"
    assert decision.verdict.verdict == "ask_user"
    assert provider.calls


def test_missing_auditor_upgrades_sensitive_action_to_user(tmp_path):
    prompts: list[str] = []
    gate = AuditGate(
        auditor=None,
        confirm=lambda action, prompt, mandatory=False: (
            prompts.append(prompt) or True
        ),
        workspace_roots=[str(tmp_path)],
        project_root=str(tmp_path),
    )
    decision = gate.review(
        Action("terminal.exec", {"command": "dir"}),
        PolicyDecision("allow", "allowed"),
    )
    assert decision.allowed is True
    assert decision.verdict.source == "fallback"
    assert prompts


def test_toctou_recheck_blocks_changed_operation(tmp_path):
    def mutate(
        action: Action, prompt: str, mandatory: bool = False
    ) -> bool:
        action.args["command"] = "rm -r other-project"
        return True

    gate = AuditGate(
        auditor=None,
        confirm=mutate,
        workspace_roots=[str(tmp_path)],
        project_root=str(tmp_path),
    )
    decision = gate.review(
        Action("terminal.exec", {"command": "rm -r build"}),
        PolicyDecision("allow", "allowed"),
    )
    assert decision.allowed is False
    assert "发生变化" in decision.reason


def test_confirm_all_mode_asks_for_typing_but_not_scrolling(tmp_path):
    gate, provider = _gate(tmp_path, mode="confirm_all")
    typed = gate.review(
        Action("desktop.act", {"action": "type", "text": "hello"}),
        PolicyDecision("allow", "allowed"),
    )
    scrolled = gate.review(
        Action("desktop.act", {"action": "scroll", "amount": 1}),
        PolicyDecision("allow", "allowed"),
    )
    assert typed.allowed is True
    assert typed.user_confirmed is True
    assert scrolled.allowed is True
    assert scrolled.user_confirmed is False
    assert provider.calls == []


def test_ai_verdicts_are_cached_for_identical_operations(tmp_path):
    gate, provider = _gate(tmp_path, verdict="allow")
    policy = PolicyDecision("allow", "allowed")
    action = Action("terminal.exec", {"command": "rm -r build"})
    first = gate.review(action, policy)
    second = gate.review(action, policy)
    assert first.allowed and second.allowed
    assert len(provider.calls) == 1
    assert second.verdict.latency_ms == 0


def test_destructive_click_uses_element_name(tmp_path):
    observation = Observation(
        step=1,
        elements=[Element("e9", "MenuItem", "Delete project")],
    )
    descriptor = normalize_operation(
        Action("desktop.act", {"action": "click", "element_id": "e9"}),
        observation=observation,
        workspace_roots=[str(tmp_path)],
        project_root=str(tmp_path),
    )
    sensitivity = detect_sensitivity(descriptor)
    assert "ui_destructive" in descriptor.effects
    assert sensitivity.sensitive is True
    assert "data_loss" in sensitivity.categories


def test_declared_sensitivity_is_union_with_rules(tmp_path):
    descriptor = normalize_operation(
        Action(
            "desktop.act",
            {
                "action": "scroll",
                "sensitive": True,
                "sensitive_categories": ["privacy"],
            },
        ),
        workspace_roots=[str(tmp_path)],
        project_root=str(tmp_path),
    )
    sensitivity = detect_sensitivity(descriptor)
    assert descriptor.declared_sensitive is True
    assert sensitivity.sensitive is True
    assert "privacy" in sensitivity.categories


def test_audit_control_keys_do_not_reach_the_descriptor(tmp_path):
    descriptor = normalize_operation(
        Action(
            "terminal.exec",
            {
                "command": "rm -r build",
                "audit_note": "runtime injected",
                "sensitive": True,
            },
        ),
        workspace_roots=[str(tmp_path)],
        project_root=str(tmp_path),
    )
    assert "audit_note" not in descriptor.args
    assert "sensitive" not in descriptor.args
    assert descriptor.audit_note == "runtime injected"


def test_cross_project_read_requires_user_even_if_ai_allows(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "other" / "notes.txt"
    outside.parent.mkdir()
    outside.write_text("another project", encoding="utf-8")
    prompts: list[str] = []
    gate, _ = _gate(
        workspace, verdict="allow", confirm=True, confirm_calls=prompts
    )
    decision = gate.review(
        Action("terminal.exec", {"command": f"type {outside}"}),
        PolicyDecision("allow", "allowed"),
    )
    assert decision.allowed is True
    assert decision.user_confirmed is True
    assert decision.verdict.verdict == "ask_user"
    assert decision.sensitivity.requires_user is True
    assert "cross_project" in decision.sensitivity.categories
    assert prompts and "可能影响：" in prompts[0]


def test_user_data_path_requires_user(tmp_path, monkeypatch):
    home = tmp_path / "home"
    documents = home / "Documents"
    documents.mkdir(parents=True)
    target = documents / "report.txt"
    target.write_text("user data", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(home))
    workspace = tmp_path / "project"
    workspace.mkdir()
    prompts: list[str] = []
    gate, _ = _gate(
        workspace, verdict="allow", confirm=True, confirm_calls=prompts
    )
    decision = gate.review(
        Action("terminal.exec", {"command": f"type {target}"}),
        PolicyDecision("allow", "allowed"),
    )
    assert decision.user_confirmed is True
    assert decision.sensitivity.requires_user is True
    assert "user_data" in decision.sensitivity.categories


def test_user_review_is_mandatory_for_cross_project(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "other" / "notes.txt"
    outside.parent.mkdir()
    outside.write_text("another project", encoding="utf-8")
    flags: list[bool] = []
    gate = AuditGate(
        auditor=TopLevelAuditor(StubAuditProvider(verdict="allow")),
        confirm=lambda action, prompt, mandatory=False: (
            flags.append(mandatory) or True
        ),
        workspace_roots=[str(workspace)],
        project_root=str(workspace),
    )
    decision = gate.review(
        Action("terminal.exec", {"command": f"type {outside}"}),
        PolicyDecision("allow", "allowed"),
    )
    assert decision.allowed is True
    assert flags == [True]


def test_ai_allow_is_escalated_when_scope_reports_cross_project(tmp_path):
    prompts: list[str] = []
    gate, _ = _gate(
        tmp_path,
        verdict="allow",
        confirm=True,
        confirm_calls=prompts,
        scope={"inside_project": False, "cross_project": True},
        categories=["cross_project"],
    )
    decision = gate.review(
        Action("terminal.exec", {"command": "rm -r build"}),
        PolicyDecision("allow", "allowed"),
    )
    assert decision.verdict.verdict == "ask_user"
    assert decision.user_confirmed is True
    assert "必须由用户确认" in decision.verdict.reason
    assert prompts


def test_auditor_never_receives_task_goal_or_rationale(tmp_path):
    provider = StubAuditProvider(verdict="allow")
    auditor = TopLevelAuditor(provider)
    descriptor = normalize_operation(
        Action(
            "terminal.exec",
            {"command": "rm -r build"},
            rationale="the secret goal is to wipe the user's projects",
        ),
        workspace_roots=[str(tmp_path)],
        project_root=str(tmp_path),
    )
    sensitivity = detect_sensitivity(descriptor)
    auditor.audit(
        descriptor,
        {"cwd": str(tmp_path)},
        PolicyDecision("allow", "allowed"),
        sensitivity,
    )
    request = json.loads(provider.calls[0]["user"])
    dumped = json.dumps(request, ensure_ascii=False)
    assert "secret goal" not in dumped
    assert "rationale" not in dumped
    assert request["operation"]["capability"] == "terminal.exec"


def test_auditor_rejects_invalid_verdict(tmp_path):
    provider = StubAuditProvider()
    provider.complete = lambda system, user, max_tokens=800: "not json"
    auditor = TopLevelAuditor(provider)
    descriptor = normalize_operation(
        Action("terminal.exec", {"command": "dir"}),
        workspace_roots=[str(tmp_path)],
        project_root=str(tmp_path),
    )
    verdict = auditor.audit(
        descriptor,
        {},
        PolicyDecision("allow", "allowed"),
        detect_sensitivity(descriptor),
    )
    assert verdict.verdict == "ask_user"
    assert verdict.source == "fallback"


def test_fallback_verdict_is_always_ask_user():
    verdict = fallback_verdict("boom")
    assert verdict.verdict == "ask_user"
    assert verdict.risk == "high"


def test_read_only_terminal_command_is_not_mutating(tmp_path):
    descriptor = normalize_operation(
        Action("terminal.exec", {"command": "git status"}),
        workspace_roots=[str(tmp_path)],
        project_root=str(tmp_path),
    )
    assert descriptor.effects == ["read"]
    assert descriptor.mutating is False
    assert detect_sensitivity(descriptor).sensitive is True


def test_rule_verdict_reports_scope(tmp_path):
    descriptor = normalize_operation(
        Action("file.transfer", {"direction": "upload", "source": str(tmp_path / "a.txt")}),
        workspace_roots=[str(tmp_path)],
        project_root=str(tmp_path),
    )
    verdict = rule_verdict(descriptor, detect_sensitivity(descriptor))
    assert verdict.verdict == "ask_user"
    assert verdict.scope["paths"]


class VisionAuditProvider(StubAuditProvider):
    vision = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.images: list[str | None] = []

    def complete(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 800,
        image_path: str | None = None,
    ) -> str:
        self.images.append(image_path)
        return super().complete(system, user, max_tokens=max_tokens)


def _vision_gate(tmp_path, *, verdict: str = "ask_user", confirm: bool = True):
    provider = VisionAuditProvider(verdict=verdict)
    calls: list[tuple[str, bool]] = []

    def confirm_callback(
        action: Action, prompt: str, mandatory: bool = False
    ) -> bool:
        calls.append((prompt, mandatory))
        return confirm

    gate = AuditGate(
        auditor=TopLevelAuditor(provider, model="vision-audit"),
        confirm=confirm_callback,
        workspace_roots=[str(tmp_path)],
        project_root=str(tmp_path),
    )
    return gate, provider, calls


def test_commit_click_is_reviewed_with_vision_and_screenshot(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    gate, provider, calls = _vision_gate(tmp_path)
    observation = Observation(
        step=1,
        elements=[Element("e1", "Button", "Publish")],
        screenshot_path=str(shot),
    )

    decision = gate.review(
        Action("desktop.act", {"action": "click", "element_id": "e1"}),
        PolicyDecision("allow", "allowed"),
        observation,
        screenshot_path=str(shot),
    )

    assert decision.allowed is True
    assert decision.rollback_review is True
    assert decision.verdict.visual is True
    assert provider.images == [str(shot)]
    assert calls and calls[0][1] is False
    assert "顶部 AI 已对照屏幕截图审查" in calls[0][0]


def test_commit_click_without_vision_model_forces_mandatory_user(tmp_path):
    prompts: list[tuple[str, bool]] = []

    def confirm_callback(
        action: Action, prompt: str, mandatory: bool = False
    ) -> bool:
        prompts.append((prompt, mandatory))
        return True

    gate = AuditGate(
        auditor=TopLevelAuditor(StubAuditProvider(verdict="allow")),
        confirm=confirm_callback,
        workspace_roots=[str(tmp_path)],
        project_root=str(tmp_path),
    )
    observation = Observation(
        step=1, elements=[Element("e1", "Button", "Delete")]
    )

    decision = gate.review(
        Action("desktop.act", {"action": "click", "element_id": "e1"}),
        PolicyDecision("allow", "allowed"),
        observation,
    )

    assert decision.allowed is True
    assert decision.user_confirmed is True
    assert decision.rollback_review is True
    assert prompt_mandatory(prompts)
    assert "未能完成识图审查" in prompts[0][0]


def prompt_mandatory(prompts: list[tuple[str, bool]]) -> bool:
    return bool(prompts) and prompts[0][1] is True


def test_vision_auditor_without_screenshot_does_not_guess(tmp_path):
    gate, provider, calls = _vision_gate(tmp_path)
    observation = Observation(
        step=1, elements=[Element("e1", "Button", "Submit")]
    )

    decision = gate.review(
        Action("desktop.act", {"action": "click", "element_id": "e1"}),
        PolicyDecision("allow", "allowed"),
        observation,
        screenshot_path=None,
    )

    assert decision.allowed is True
    assert decision.user_confirmed is True
    assert provider.images == []
    assert prompt_mandatory(calls)
    assert "无法获取屏幕截图" in calls[0][0]


def test_desktop_commit_and_destructive_actions_need_visual_review(tmp_path):
    from xm_agent.guard.operation import (
        needs_rollback_review,
        needs_visual_review,
    )

    for label in ("Publish", "发送", "Delete project"):
        descriptor = normalize_operation(
            Action("desktop.act", {"action": "click", "label": label}),
            workspace_roots=[str(tmp_path)],
            project_root=str(tmp_path),
        )
        sensitivity = detect_sensitivity(descriptor)
        assert needs_rollback_review(descriptor, sensitivity)
        assert needs_visual_review(descriptor, sensitivity)


def test_terminal_delete_needs_review_but_no_screenshot(tmp_path):
    from xm_agent.guard.operation import (
        needs_rollback_review,
        needs_visual_review,
    )

    descriptor = normalize_operation(
        Action("terminal.exec", {"command": "rm -r build"}),
        workspace_roots=[str(tmp_path)],
        project_root=str(tmp_path),
    )
    assert needs_rollback_review(descriptor)
    assert not needs_visual_review(descriptor)


def test_explicit_rollback_risk_flag_is_honored(tmp_path):
    from xm_agent.guard.operation import needs_rollback_review

    descriptor = normalize_operation(
        Action(
            "desktop.act",
            {"action": "click", "label": "OK", "rollback_risk": True},
        ),
        workspace_roots=[str(tmp_path)],
        project_root=str(tmp_path),
    )
    assert "ui_commit" in descriptor.effects
    assert needs_rollback_review(descriptor)
