"""FORK 2026-11-06: explanation drafts after coach edits (PROC-80)."""

from tp_mcp.tools.lo_expl_sync import compose_description, draft_explanation, number_substitutions
from tp_mcp.tools.lo_sync import diff_rows, explanation_drafts


def test_range_and_rest_pairs():
    old = "- 熱身15分鐘\n- 6分鐘@235~248W+3分鐘@150~170W, 4組\n- 間休 20秒"
    new = "- 熱身15分鐘\n- 6分鐘@230~242W+3分鐘@150~170W, 4組\n- 間休 30秒"
    pairs = dict(number_substitutions(old, new))
    assert pairs["235~248W"] == "230~242W" and pairs["20秒"] == "30秒"
    assert "150~170W" not in pairs


def test_draft_keeps_explanation_style():
    old = "- 4×5分鐘@235~248W"
    new = "- 4×5分鐘@230~242W"
    expl = "－－\n高段守在 235～248W，最後一組可推到上緣。"
    d = draft_explanation(old, new, expl, stale_numbers=["235~248W"])
    assert "230～242W" in d["draft"] and d["changed"] and not d["unresolved"]


def test_unpaired_stays_unresolved():
    old = "- 3×1.6公里@4:20~4:10/km"
    new = "- 4×1.2公里@4:20~4:10/km\n- 加一段"
    d = draft_explanation(old, new, "－－\n每趟1.6公里", stale_numbers=["1.6公里"])
    assert d["draft"].endswith("每趟1.2公里")


def test_date_move_shifts_race_day_count_and_flags_weekday():
    d = draft_explanation("- 輕鬆跑40分鐘", "- 輕鬆跑40分鐘", "－－\n比賽後第 2 天，週三放輕鬆。D+2",
                          date_from="2026-11-10", date_to="2026-11-11")
    assert "比賽後第 3 天" in d["draft"] and "D+3" in d["draft"]
    assert d["check_phrases"] == ["週三"]


def test_no_partial_number_hits():
    d = draft_explanation("- 5分鐘@200W", "- 5分鐘@210W", "－－\n1200W 不是 200W")
    assert d["draft"] == "－－\n1200W 不是 210W"


def test_compose_keeps_separator_whitespace():
    assert compose_description("- a", "－－\nb", "- old\n\n") == "- a\n\n－－\nb"


def test_explanation_drafts_from_diff():
    before = {"1": {"id": "1", "date": "2026-11-10", "sport": "Bike", "title": "閾值",
                    "description": "- 3×10分鐘@235~248W\n－－\n守 235~248W。"}}
    after = [{"id": "1", "date": "2026-11-10", "sport": "Bike", "title": "閾值",
              "description": "- 3×10分鐘@230~242W\n－－\n守 235~248W。"}]
    diff = diff_rows(before, after, "2026-11-09", "2026-11-15")
    drafts = explanation_drafts(before, after, diff, {})
    assert drafts and drafts[0]["draft_description"].endswith("守 230~242W。")
    assert drafts[0]["body_source"] == "coach_body"


def test_explanation_drafts_uses_rendered_body_when_graph_changed():
    before = {"1": {"id": "1", "date": "2026-11-10", "sport": "Bike", "title": "閾值",
                    "description": "- 3×10分鐘@235~248W\n－－\n守 235~248W。"}}
    after = [dict(before["1"])]
    diff = {"changed": [{"id": "1", "date": "2026-11-10", "title": "閾值", "changes": {"structure": True}}]}
    stale = {"1": {"suggested_body": "- 3×10分鐘@230~242W"}}
    d = explanation_drafts(before, after, diff, stale)[0]
    assert d["body_source"] == "rendered_from_graph" and "230~242W" in d["draft_description"]
