from __future__ import annotations

from typing import Any, Callable


def open_window(
    url: str,
    debug: bool = False,
    on_ready: Callable[[Any], None] | None = None,
) -> int:
    try:
        import webview
    except ImportError:
        print(
            "[ui] pywebview is not installed. Run:\n"
            "  .venv\\Scripts\\python.exe -m pip install pywebview\n"
            "or start in browser mode with --server-only",
        )
        return 1
    window = webview.create_window(
        "XM Agent",
        url,
        width=1180,
        height=780,
        min_size=(920, 640),
        background_color="#0f1115",
    )

    def _ready(*args: Any) -> None:
        # pywebview hands the window to the start callback; fall back to the
        # object returned by create_window if that ever changes.
        handle = args[0] if args else window
        if on_ready is not None:
            try:
                on_ready(handle)
            except Exception as exc:  # pragma: no cover - window backend
                print(f"[ui] window callback failed: {exc}")

    webview.start(_ready, debug=debug)
    return 0


__all__ = ["open_window"]
