from __future__ import annotations


def open_window(url: str, debug: bool = False) -> int:
    try:
        import webview
    except ImportError:
        print(
            "[ui] pywebview is not installed. Run:\n"
            "  .venv\\Scripts\\python.exe -m pip install pywebview\n"
            "or start in browser mode with --server-only",
        )
        return 1
    webview.create_window(
        "XM Agent",
        url,
        width=1180,
        height=780,
        min_size=(920, 640),
        background_color="#0f1115",
    )
    webview.start(debug=debug)
    return 0


__all__ = ["open_window"]
