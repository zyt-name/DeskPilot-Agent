from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..model_caps import vision_from_name


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    protocol: str
    base_url: str
    models_style: str = "openai"
    requires_key: bool = True
    note: str = ""
    fallback_base_url: str = ""
    fallback_models_style: str = ""


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    vendor: str
    docs_url: str
    plans: tuple[Plan, ...]
    description: str = ""


def _p(
    id: str,
    name: str,
    base_url: str,
    protocol: str = "openai",
    models_style: str = "openai",
    requires_key: bool = True,
    note: str = "",
    fallback_base_url: str = "",
    fallback_models_style: str = "",
) -> Plan:
    return Plan(
        id=id,
        name=name,
        protocol=protocol,
        base_url=base_url,
        models_style=models_style,
        requires_key=requires_key,
        note=note,
        fallback_base_url=fallback_base_url,
        fallback_models_style=fallback_models_style,
    )


PRESETS: tuple[Preset, ...] = (
    Preset(
        id="openai",
        name="OpenAI / GPT",
        vendor="OpenAI",
        docs_url="https://platform.openai.com/docs/models",
        description="Chat Completions 兼容接口，支持 GPT 与推理系列。",
        plans=(_p("standard", "标准 API", "https://api.openai.com/v1"),),
    ),
    Preset(
        id="anthropic",
        name="Anthropic / Claude",
        vendor="Anthropic",
        docs_url="https://docs.anthropic.com/en/docs/about-claude/models",
        description="Messages API；Claude 3 之后的模型均支持图像输入。",
        plans=(
            _p(
                "standard",
                "标准 API",
                "https://api.anthropic.com/v1",
                protocol="anthropic",
                models_style="anthropic",
            ),
        ),
    ),
    Preset(
        id="deepseek",
        name="DeepSeek",
        vendor="DeepSeek",
        docs_url="https://api-docs.deepseek.com/",
        description="OpenAI 兼容接口，chat / reasoner 两类模型。",
        plans=(_p("standard", "标准 API", "https://api.deepseek.com/v1"),),
    ),
    Preset(
        id="glm",
        name="智谱 GLM",
        vendor="Zhipu",
        docs_url="https://docs.bigmodel.cn/",
        description="标准套餐走 OpenAI 兼容端点；Coding Plan 走 Anthropic 兼容端点。",
        plans=(
            _p("cn-standard", "国内标准套餐", "https://open.bigmodel.cn/api/paas/v4"),
            _p(
                "cn-coding",
                "国内 Coding Plan",
                "https://open.bigmodel.cn/api/anthropic/v1",
                protocol="anthropic",
                models_style="anthropic",
                note="Coding Plan 使用 Anthropic 兼容端点；该端点不提供模型列表，自动回退到国内标准端点。",
                fallback_base_url="https://open.bigmodel.cn/api/paas/v4",
                fallback_models_style="openai",
            ),
            _p("global-standard", "国际站标准", "https://api.z.ai/api/paas/v4"),
            _p(
                "global-coding",
                "国际站 Coding Plan",
                "https://api.z.ai/api/anthropic/v1",
                protocol="anthropic",
                models_style="anthropic",
                note="Coding Plan 使用 Anthropic 兼容端点；该端点不提供模型列表，自动回退到国际站标准端点。",
                fallback_base_url="https://api.z.ai/api/paas/v4",
                fallback_models_style="openai",
            ),
        ),
    ),
    Preset(
        id="kimi",
        name="Kimi / Moonshot",
        vendor="Moonshot",
        docs_url="https://platform.moonshot.cn/docs",
        description="OpenAI 兼容接口，国内与国际站分开。",
        plans=(
            _p("cn", "国内站", "https://api.moonshot.cn/v1"),
            _p("global", "国际站", "https://api.moonshot.ai/v1"),
        ),
    ),
    Preset(
        id="gemini",
        name="Google Gemini",
        vendor="Google",
        docs_url="https://ai.google.dev/gemini-api/docs/models",
        description="使用 Gemini 的 OpenAI 兼容端点；模型列表走原生 v1beta 接口。",
        plans=(
            _p(
                "standard",
                "Gemini API",
                "https://generativelanguage.googleapis.com/v1beta/openai",
                models_style="gemini",
            ),
        ),
    ),
    Preset(
        id="grok",
        name="xAI Grok",
        vendor="xAI",
        docs_url="https://docs.x.ai/docs/models",
        description="OpenAI 兼容接口，视觉模型名称带 vision。",
        plans=(_p("standard", "标准 API", "https://api.x.ai/v1"),),
    ),
    Preset(
        id="opencode",
        name="OpenCode Zen",
        vendor="OpenCode",
        docs_url="https://opencode.ai/docs/zen/",
        description="Zen 网关的 OpenAI 兼容端点；套餐由账户决定，端点和模型可自行调整。",
        plans=(
            _p(
                "standard",
                "普通套餐",
                "https://opencode.ai/zen/v1",
                note="套餐差异体现在账户可用模型与计费，端点相同。",
            ),
            _p(
                "go",
                "Go 套餐",
                "https://opencode.ai/zen/v1",
                note="Go 套餐端点与普通套餐相同，可用模型以账户为准。",
            ),
        ),
    ),
    Preset(
        id="ollama",
        name="Ollama 本地",
        vendor="Local",
        docs_url="https://ollama.com/",
        description="本机 Ollama 的 OpenAI 兼容端点，默认无需密钥。",
        plans=(
            _p(
                "local",
                "本机默认",
                "http://127.0.0.1:11434/v1",
                models_style="ollama",
                requires_key=False,
            ),
        ),
    ),
    Preset(
        id="openrouter",
        name="OpenRouter",
        vendor="OpenRouter",
        docs_url="https://openrouter.ai/docs",
        description="聚合多家模型，OpenAI 兼容。",
        plans=(_p("standard", "标准 API", "https://openrouter.ai/api/v1"),),
    ),
    Preset(
        id="siliconflow",
        name="硅基流动",
        vendor="SiliconFlow",
        docs_url="https://docs.siliconflow.cn/",
        description="国内聚合平台，OpenAI 兼容。",
        plans=(_p("standard", "标准 API", "https://api.siliconflow.cn/v1"),),
    ),
    Preset(
        id="dashscope",
        name="阿里云百炼",
        vendor="Alibaba",
        docs_url="https://help.aliyun.com/zh/model-studio/",
        description="DashScope 的 OpenAI 兼容模式。",
        plans=(
            _p(
                "standard",
                "兼容模式",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
        ),
    ),
    Preset(
        id="ark",
        name="火山方舟",
        vendor="ByteDance",
        docs_url="https://www.volcengine.com/docs/82379",
        description="方舟的 OpenAI 兼容接口，模型名通常为接入点 ID。",
        plans=(
            _p("standard", "兼容接口", "https://ark.cn-beijing.volces.com/api/v3"),
        ),
    ),
    Preset(
        id="minimax",
        name="MiniMax",
        vendor="MiniMax",
        docs_url="https://platform.minimaxi.com/document",
        description="OpenAI 兼容接口。",
        plans=(_p("standard", "标准 API", "https://api.minimax.chat/v1"),),
    ),
    Preset(
        id="mistral",
        name="Mistral",
        vendor="Mistral",
        docs_url="https://docs.mistral.ai/",
        description="OpenAI 兼容接口，Pixtral 支持图像。",
        plans=(_p("standard", "标准 API", "https://api.mistral.ai/v1"),),
    ),
    Preset(
        id="groq",
        name="Groq",
        vendor="Groq",
        docs_url="https://console.groq.com/docs/models",
        description="OpenAI 兼容接口，推理速度快。",
        plans=(_p("standard", "标准 API", "https://api.groq.com/openai/v1"),),
    ),
    Preset(
        id="together",
        name="Together AI",
        vendor="Together",
        docs_url="https://docs.together.ai/docs/models",
        description="OpenAI 兼容接口，模型覆盖广。",
        plans=(_p("standard", "标准 API", "https://api.together.xyz/v1"),),
    ),
    Preset(
        id="custom",
        name="自定义 / 其他",
        vendor="Custom",
        docs_url="",
        description="任何 OpenAI 兼容或 Anthropic 兼容端点，本地网关也可以。",
        plans=(
            _p("openai", "OpenAI 兼容", "", protocol="openai"),
            _p(
                "anthropic",
                "Anthropic 兼容",
                "",
                protocol="anthropic",
                models_style="anthropic",
            ),
        ),
    ),
)

_BY_ID = {preset.id: preset for preset in PRESETS}

_NON_CHAT_MARKERS = (
    "embedding",
    "embed-",
    "rerank",
    "moderation",
    "whisper",
    "tts",
    "transcribe",
    "speech",
    "dall-e",
    "stable-diffusion",
    "imagen",
    "veo",
    "aqa",
    "bge-",
    "clip-",
)


def preset_by_id(preset_id: str) -> Preset | None:
    return _BY_ID.get(preset_id)


def plan_by_id(preset_id: str, plan_id: str) -> Plan | None:
    preset = _BY_ID.get(preset_id)
    if preset is None:
        return None
    for plan in preset.plans:
        if plan.id == plan_id:
            return plan
    return None


def preset_endpoint(preset_id: str, plan_id: str) -> dict[str, Any] | None:
    """Authoritative endpoint fields for a preset plan; custom stays user-defined."""
    if not preset_id or preset_id == "custom":
        return None
    plan = plan_by_id(preset_id, plan_id)
    if plan is None:
        return None
    return {
        "protocol": plan.protocol,
        "base_url": plan.base_url.rstrip("/"),
        "models_style": plan.models_style,
        "requires_key": plan.requires_key,
    }


def is_vision_model(model_id: str) -> bool:
    return vision_from_name(model_id)


def is_chat_model(model_id: str) -> bool:
    name = model_id.casefold()
    return not any(marker in name for marker in _NON_CHAT_MARKERS)


def catalog_dict() -> list[dict[str, Any]]:
    return [
        {
            **{key: value for key, value in asdict(preset).items() if key != "plans"},
            "plans": [asdict(plan) for plan in preset.plans],
        }
        for preset in PRESETS
    ]


__all__ = [
    "PRESETS",
    "Plan",
    "Preset",
    "catalog_dict",
    "is_chat_model",
    "is_vision_model",
    "plan_by_id",
    "preset_by_id",
    "preset_endpoint",
]
