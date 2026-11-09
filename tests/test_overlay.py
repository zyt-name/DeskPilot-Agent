import os
from itertools import pairwise

import pytest

from xm_agent.ui import overlay


def test_status_text_matches_pause_state():
    assert overlay._status_text(False) == "正在操控 · 按 F8 暂停"
    assert overlay._status_text(True) == "已暂停 · 按 F8 继续"


def test_breath_level_is_slow_and_bounded():
    levels = [overlay._breath_level(frame) for frame in range(overlay._BREATH_FRAMES)]
    assert min(levels) == pytest.approx(overlay._LEVEL_MIN)
    assert max(levels) == pytest.approx(overlay._LEVEL_MAX)
    steps = [abs(later - earlier) for earlier, later in pairwise(levels)]
    assert max(steps) < 0.12


def test_edge_palette_cycles_through_colours():
    palette = overlay._RUN_PALETTE
    step = overlay._COLOR_STEP_FRAMES
    assert overlay._palette_color(0) == palette[0]
    assert overlay._palette_color(step) == palette[1]
    assert overlay._palette_color(step * 2) == palette[2]
    assert overlay._palette_color(step * 3) == palette[3]
    assert overlay._palette_color(step * 4) == palette[0]


def test_palette_blends_between_colours():
    step = overlay._COLOR_STEP_FRAMES
    blend = overlay._palette_color(step - 1)
    assert overlay._RUN_PALETTE[0] != blend != overlay._RUN_PALETTE[1]
    assert all(
        0 <= channel <= 255 for channel in blend
    )


def test_edge_frame_fades_away_from_the_border():
    height = overlay._GLOW + 4
    frame = overlay._edge_frame("top", 8, height, (80, 170, 255), 1.0)
    assert len(frame) == 8 * height * 4
    first_alpha = frame[3]
    last_row = frame[-8 * 4 :]
    assert first_alpha > 150
    assert last_row[3] == 0
    left = overlay._edge_frame("left", height, 8, (80, 170, 255), 1.0)
    assert left[:4] == overlay._edge_frame("top", 1, height, (80, 170, 255), 1.0)[:4]
    assert left[-4:] == b"\x00\x00\x00\x00"


def test_set_paused_tracks_state_without_a_window():
    indicator = overlay.ScreenIndicator(enabled=False)
    assert indicator.paused is False
    indicator.set_paused(True)
    assert indicator.paused is True
    indicator.set_paused(True)
    indicator.show()
    assert indicator.paused is False
    assert indicator.start() is False


@pytest.mark.skipif(os.name != "nt", reason="badge text is rasterized with GDI")
def test_badge_frame_renders_glyphs_and_widths():
    running = overlay._badge_frame(False, 1.0)
    paused = overlay._badge_frame(True, 1.0)
    assert running[1] > 100 and running[2] > 20
    assert len(running[0]) == running[1] * running[2] * 4
    assert running[1] > paused[1]
    assert any(running[0][index] for index in range(3, len(running[0]), 4))
    wide = overlay._badge_frame(False, 1.0, 400)
    assert wide[1] == 400
