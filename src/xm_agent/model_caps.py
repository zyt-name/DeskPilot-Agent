"""Model capability detection from provider metadata and official naming.

Vision support is never a user preference: it is read from the provider's model
list when that list exposes modality/capability metadata, and otherwise derived
from the vendor's official model naming. Unknown models stay text-only.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

SOURCE_PROVIDER = "provider"
SOURCE_NAME = "model"
SOURCE_UNKNOWN = "unknown"

_VISION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # OpenAI
        r"^gpt-(4o|4\.1|4\.5|4-turbo|4-vision|5)(?=$|[-_.@])",
        r"^chatgpt-4o",
        r"^o1(?!-(mini|preview))",
        r"^o3(?!-mini)",
        r"^o4(-mini)?(?=$|[-_.@])",
        # Anthropic
        r"^claude-(3|4|sonnet|opus|haiku)(?=$|[-_.@])",
        # Google
        r"^gemini-(?:(?:1\.[5-9]|[2-9])(?=$|[-_.@])|exp(?=$|[-_.@])|pro-vision|vision)",
        r"^gemma-3",
        r"^paligemma",
        # Meta
        r"^llama-?4(?=$|[-_.@])",
        r"^llama.*vision",
        r"^llava",
        r"^bakllava",
        r"^moondream",
        r"^idefics",
        r"^molmo",
        # Mistral / Microsoft
        r"^pixtral",
        r"^mistral-small-3\.[12]",
        r"^mistral.*vision",
        r"^phi-.*vision",
        r"^phi-4-multimodal",
        # Zhipu
        r"^glm-\d+(?:\.\d+)?v(?=$|[-_.@])",
        # Alibaba
        r"^qwen.*(vl|vision|omni)",
        r"^qvq",
        # Moonshot / DeepSeek / MiniMax
        r"^kimi.*(vision|vl)",
        r"^moonshot.*(vision|vl)",
        r"^kimi-latest$",
        r"^deepseek.*(vl|vision)",
        r"^minimax.*(vl|vision)",
        # xAI / ByteDance / StepFun / 01.AI
        r"^grok.*vision",
        r"^grok-4",
        r"^doubao.*(vision|vl)",
        r"^doubao-seed-1[.-]6",
        r"^step-1v",
        r"^yi-vision",
        # Cohere / Amazon / IBM / Tencent / Baidu / iFlytek
        r"^command-a-vision",
        r"^nova-(pro|lite|premier)",
        r"^granite.*vision",
        r"^hunyuan.*vision",
        r"^ernie.*(vl|vision)",
        r"^spark.*vision",
        # Open VLM families
        r"^internvl",
        r"^minicpm.*v",
        r"^cogvlm",
        r"^ovis",
        r"^emu3",
        r"^chameleon",
    )
)

_IMAGE_MARKERS = {
    "image",
    "images",
    "image_url",
    "vision",
    "visual",
    "multimodal",
}

_VISION_ONLY_MARKERS = {"vision", "visual", "multimodal"}


def normalize_model_id(model_id: str) -> str:
    name = model_id.strip().casefold()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name.split(":", 1)[0]


def vision_from_name(model_id: str) -> bool:
    name = normalize_model_id(model_id)
    if not name:
        return False
    return any(pattern.search(name) for pattern in _VISION_PATTERNS)


def _modality_flag(
    values: Any, markers: set[str] = _IMAGE_MARKERS
) -> bool | None:
    if not isinstance(values, (list, tuple, set)):
        return None
    normalized = {str(value).strip().casefold() for value in values}
    if not normalized:
        return None
    return bool(normalized & markers)


def reported_vision(entry: dict[str, Any]) -> bool | None:
    """Read modality metadata from one entry of a provider model list."""
    if not isinstance(entry, dict):
        return None
    architecture = entry.get("architecture")
    if isinstance(architecture, dict):
        flag = _modality_flag(architecture.get("input_modalities"))
        if flag is not None:
            return flag
    for key in (
        "input_modalities",
        "inputModalities",
        "supported_input_modalities",
    ):
        flag = _modality_flag(entry.get(key))
        if flag is not None:
            return flag
    for key in (
        "supports_image_in",
        "supports_image_input",
        "supports_vision",
    ):
        value = entry.get(key)
        if isinstance(value, bool):
            return value
    modality = entry.get("modality")
    if isinstance(modality, str) and "->" in modality:
        inputs = modality.split("->", 1)[0]
        if _modality_flag(re.split(r"[+,]", inputs)):
            return True
    capabilities = entry.get("capabilities")
    if isinstance(capabilities, list):
        if _modality_flag(capabilities, _VISION_ONLY_MARKERS):
            return True
    if isinstance(capabilities, dict):
        for key in ("vision", "image_input"):
            value = capabilities.get(key)
            if isinstance(value, bool):
                return value
            if isinstance(value, dict) and isinstance(
                value.get("supported"), bool
            ):
                return value["supported"]
        flag = _modality_flag(capabilities.get("input"))
        if flag is not None:
            return flag
    for key in ("features", "input_features"):
        if _modality_flag(entry.get(key), _VISION_ONLY_MARKERS):
            return True
    return None


def families_vision(families: Iterable[str] | None) -> bool | None:
    if not families:
        return None
    normalized = {str(family).strip().casefold() for family in families}
    if normalized & {"clip", "vision", "visual"}:
        return True
    return None


def resolve_vision_detail(
    model_id: str,
    *,
    reported: bool | None = None,
    families: Iterable[str] | None = None,
) -> tuple[bool, str]:
    if isinstance(reported, bool):
        return reported, SOURCE_PROVIDER
    family_flag = families_vision(families)
    if family_flag is not None:
        return family_flag, SOURCE_PROVIDER
    if vision_from_name(model_id):
        return True, SOURCE_NAME
    return False, SOURCE_UNKNOWN


def resolve_vision(
    model_id: str,
    *,
    reported: bool | None = None,
    families: Iterable[str] | None = None,
) -> bool:
    return resolve_vision_detail(
        model_id, reported=reported, families=families
    )[0]


__all__ = [
    "SOURCE_NAME",
    "SOURCE_PROVIDER",
    "SOURCE_UNKNOWN",
    "families_vision",
    "normalize_model_id",
    "reported_vision",
    "resolve_vision",
    "resolve_vision_detail",
    "vision_from_name",
]
