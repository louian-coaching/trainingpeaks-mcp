"""FORK 2026-11-12: training-plan authoring tools (lo_plans.py).

The fake below reproduces what the live test on 2026-11-12 showed about
/plans/v1: startDate follows the earliest workout, a PUT replaces the whole
workout (so a partial PUT blanks the rest), structure goes up as a string and
comes back as an object.
"""

import json
import re
from datetime import date, timedelta
from json import loads as _loads
from unittest.mock import patch

import pytest

from tp_mcp import server
from tp_mcp.client.http import APIResponse, ErrorCode
from tp_mcp.tools.lo_plans import (
    lo_plan_add_workouts,
    lo_plan_create,
    lo_plan_delete_workouts,
    lo_plan_get_workouts,
    lo_plan_update_meta,
    lo_plan_update_workout,
)

_NATIVE = {
    "structure": [], "polyline": [], "primaryLengthMetric": "duration",
    "primaryIntensityMetric": "percentOfFtp", "primaryIntensityTargetOrRange": "range",
}


def _monday(d):
    return d - timedelta(days=d.weekday())


class FakeTP:
    def __init__(self):
        self.plans = {}
        self.workouts = {}
        self.writes = []  # (method, endpoint, body)
        self._plan_seq = 700000
        self._workout_seq = 9000
        self.lose_answer_for = set()  # titles whose POST lands but answers NETWORK_ERROR
        self.reject = set()  # titles whose POST is refused

    def add_plan(self, title="Plan", is_public=False, workouts=()):
        self._plan_seq += 1
        pid = self._plan_seq
        self.plans[pid] = {
            "planId": pid, "planPersonId": pid + 5_000_000, "title": title, "description": None,
            "planLanguage": None, "isPublic": is_public, "isSearchable": is_public,
            "price": 80.0 if is_public else None, "isDynamic": False,
        }
        self.workouts[pid] = {}
        for w in workouts:
            self._store(pid, dict(w))
        return pid

    def _store(self, pid, w):
        self._workout_seq += 1
        w["workoutId"] = self._workout_seq
        if isinstance(w.get("structure"), str):
            w["structure"] = _loads(w["structure"])
        self.workouts[pid][w["workoutId"]] = w
        return w

    def _view(self, pid):
        plan = dict(self.plans[pid])
        days = sorted(date.fromisoformat(w["workoutDay"][:10]) for w in self.workouts[pid].values())
        if days:
            span = (days[-1] - days[0]).days + 1
            plan.update(startDate=f"{days[0].isoformat()}T00:00:00", endDate=f"{days[-1].isoformat()}T00:00:00",
                        dayCount=span, weekCount=(_monday(days[-1]) - _monday(days[0])).days // 7 + 1)
        else:
            plan.update(startDate=None, endDate=None, dayCount=None, weekCount=0)
        plan["workoutCount"] = len(days)
        return plan

    async def get(self, endpoint, params=None):
        if endpoint == "/plans/v1/plans":
            return APIResponse(success=True, data=[self._view(p) for p in self.plans])
        m = re.fullmatch(r"/plans/v1/plans/(\d+)", endpoint)
        if m:
            pid = int(m.group(1))
            if pid not in self.plans:
                return APIResponse(success=False, error_code=ErrorCode.NOT_FOUND, message="Resource not found.")
            return APIResponse(success=True, data=self._view(pid))
        m = re.fullmatch(r"/plans/v1/plans/(\d+)/workouts/(\d{4}-\d\d-\d\d)/(\d{4}-\d\d-\d\d)", endpoint)
        assert m, endpoint
        pid, lo, hi = int(m.group(1)), m.group(2), m.group(3)
        rows = [dict(w) for w in self.workouts[pid].values() if lo <= w["workoutDay"][:10] <= hi]
        return APIResponse(success=True, data=rows)

    async def post(self, endpoint, json=None):
        self.writes.append(("POST", endpoint, json))
        if endpoint == "/plans/v1/plans":
            pid = self.add_plan(title=json["title"])
            return APIResponse(success=True, data=self._view(pid))
        m = re.fullmatch(r"/plans/v1/plans/(\d+)/workouts", endpoint)
        assert m, endpoint
        if json["title"] in self.reject:
            return APIResponse(success=False, error_code=ErrorCode.API_ERROR, message="API error: 400")
        stored = self._store(int(m.group(1)), dict(json))
        if json["title"] in self.lose_answer_for:
            return APIResponse(success=False, error_code=ErrorCode.NETWORK_ERROR, message="timeout")
        return APIResponse(success=True, data=dict(stored))

    async def put(self, endpoint, json=None):
        self.writes.append(("PUT", endpoint, json))
        m = re.fullmatch(r"/plans/v1/plans/(\d+)/workouts/(\d+)", endpoint)
        if m:
            pid, wid = int(m.group(1)), int(m.group(2))
            w = dict(json)  # whole-object replace: omitted fields are gone
            w["workoutId"] = wid
            if isinstance(w.get("structure"), str):
                w["structure"] = _loads(w["structure"])
            self.workouts[pid][wid] = w
            return APIResponse(success=True, data=dict(w))
        m = re.fullmatch(r"/plans/v1/plans/(\d+)", endpoint)
        assert m, endpoint
        pid = int(m.group(1))
        for key in ("title", "description", "planLanguage", "isPublic", "isSearchable", "price"):
            if key in json:
                self.plans[pid][key] = json[key]
        return APIResponse(success=True, data=self._view(pid))

    async def delete(self, endpoint):
        self.writes.append(("DELETE", endpoint, None))
        m = re.fullmatch(r"/plans/v1/plans/(\d+)/workouts/(\d+)", endpoint)
        assert m, endpoint
        self.workouts[int(m.group(1))].pop(int(m.group(2)), None)
        return APIResponse(success=True, data=True)


@pytest.fixture
def tp():
    fake = FakeTP()
    with patch("tp_mcp.tools.lo_plans.TPClient") as cls:
        cls.return_value.__aenter__.return_value = fake
        yield fake


def _w(day, type_id=3, title="Run", **extra):
    return {"athleteId": 1, "workoutDay": f"{day}T00:00:00", "workoutTypeValueId": type_id,
            "title": title, **extra}


async def test_create_is_private_and_sets_description(tp):
    r = await lo_plan_create("8 週入門計劃", description="給第一次比賽的人")
    assert r["success"] and r["verified"] and r["plan"]["is_public"] is False
    assert tp.writes[0] == ("POST", "/plans/v1/plans", {"title": "8 週入門計劃", "isDynamic": False})
    method, endpoint, body = tp.writes[1]
    assert method == "PUT" and endpoint == f"/plans/v1/plans/{r['plan_id']}"
    assert body["description"] == "給第一次比賽的人" and body["planLanguage"] == "zh-Hant"
    assert body["planId"] == r["plan_id"]  # the whole plan object goes up


async def test_create_refuses_existing_title(tp):
    pid = tp.add_plan(title="12 週 113")
    r = await lo_plan_create(" 12 週 113 ")
    assert r["error_code"] == "DUPLICATE_TITLE" and r["plan_id"] == pid and tp.writes == []


async def test_empty_plan_needs_week_one_first(tp):
    pid = tp.add_plan()
    r = await lo_plan_add_workouts(pid, [{"week": 2, "day": 1, "sport": "Run", "title": "跑"}])
    assert r["error_code"] == "VALIDATION_ERROR" and "week 1" in r["message"] and tp.writes == []


async def test_add_to_empty_plan_anchors_on_this_weeks_monday(tp):
    pid = tp.add_plan()
    anchor = _monday(date.today())
    r = await lo_plan_add_workouts(pid, [
        {"week": 1, "day": 3, "sport": "Bike", "title": "有氧耐力", "duration_minutes": 75,
         "tss_planned": 58, "description": "課表\n－－\n說明", "structured_workout": _NATIVE},
        {"week": 2, "day": 7, "sport": "Swim", "title": "技術游", "duration_minutes": 60, "distance_km": 2.5},
    ])
    assert r["success"] and r["created"] == 2 and all(row["verified"] for row in r["rows"])
    first, second = (body for _, _, body in tp.writes)
    assert first["workoutDay"] == f"{(anchor + timedelta(days=2)).isoformat()}T00:00:00"
    assert second["workoutDay"] == f"{(anchor + timedelta(days=13)).isoformat()}T00:00:00"
    assert first["athleteId"] == tp.plans[pid]["planPersonId"] and first["workoutId"] == 0
    assert (first["workoutTypeFamilyId"], first["workoutTypeValueId"]) == (2, 2)
    assert first["totalTimePlanned"] == 1.25 and first["tssPlanned"] == 58
    assert json.loads(first["structure"]) == _NATIVE  # sent as a string
    assert second["distancePlanned"] == 2500 and "structure" not in second
    assert r["plan"]["weeks"] == 2 and r["plan"]["week1_monday"] == anchor.isoformat()


async def test_week_one_is_the_monday_week_of_the_earliest_workout(tp):
    pid = tp.add_plan(workouts=[_w("2026-11-18")])  # a Wednesday
    r = await lo_plan_add_workouts(pid, [{"week": 2, "day": 1, "sport": "Run", "title": "長跑"}])
    assert r["success"] and tp.writes[0][2]["workoutDay"] == "2026-11-23T00:00:00"
    listing = await lo_plan_get_workouts(pid)
    assert [(w["week"], w["day"]) for w in listing["workouts"]] == [(1, 3), (2, 1)]


async def test_structure_without_tss_writes_nothing(tp):
    pid = tp.add_plan()
    r = await lo_plan_add_workouts(pid, [
        {"week": 1, "day": 1, "sport": "Run", "title": "ok"},
        {"week": 1, "day": 2, "sport": "Bike", "title": "bad", "structured_workout": _NATIVE},
    ])
    assert r["error_code"] == "VALIDATION_ERROR" and r["errors"][0]["index"] == 1 and tp.writes == []


async def test_skip_if_exists_and_dry_run(tp):
    pid = tp.add_plan(workouts=[_w("2026-11-16", title="輕鬆跑")])
    items = [{"week": 1, "day": 1, "sport": "Run", "title": "輕鬆跑"},
             {"week": 1, "day": 2, "sport": "Run", "title": "輕鬆跑"}]
    dry = await lo_plan_add_workouts(pid, items, dry_run=True)
    assert [row["status"] for row in dry["rows"]] == ["skipped_exists", "would_create"] and tp.writes == []
    r = await lo_plan_add_workouts(pid, items)
    assert r["created"] == 1 and r["skipped"] == 1 and len(tp.writes) == 1


async def test_lost_answer_is_found_in_the_read_back_not_resent(tp):
    pid = tp.add_plan(workouts=[_w("2026-11-16")])
    tp.lose_answer_for.add("長跑")
    tp.reject.add("壞課")
    r = await lo_plan_add_workouts(pid, [
        {"week": 1, "day": 6, "sport": "Run", "title": "長跑", "duration_minutes": 90},
        {"week": 1, "day": 7, "sport": "Run", "title": "壞課"},
    ])
    assert [row["status"] for row in r["rows"]] == ["created", "failed"]
    assert r["rows"][0]["verified"] and r["success"] is False and r["problems"] == 1
    assert sum(1 for m, _, _ in tp.writes if m == "POST") == 2  # one attempt each


async def test_update_resends_everything_it_does_not_change(tp):
    pid = tp.add_plan(workouts=[_w("2026-11-18", 2, "有氧耐力", description="說明", totalTimePlanned=1.25,
                                   tssPlanned=58, structure=dict(_NATIVE), lastModifiedDate="x")])
    wid = next(iter(tp.workouts[pid]))
    r = await lo_plan_update_workout(pid, wid, title="有氧耐力 II")
    assert r["success"] and r["after"]["title"] == "有氧耐力 II"
    body = tp.writes[0][2]
    assert body["description"] == "說明" and body["tssPlanned"] == 58 and body["totalTimePlanned"] == 1.25
    assert json.loads(body["structure"]) == _NATIVE
    kept = tp.workouts[pid][wid]
    assert kept["description"] == "說明" and kept["structure"] == _NATIVE and kept["tssPlanned"] == 58


async def test_update_moves_clears_and_validates(tp):
    pid = tp.add_plan(workouts=[_w("2026-11-16", title="A", description="舊"), _w("2026-11-18", title="B")])
    wid = max(tp.workouts[pid])
    r = await lo_plan_update_workout(pid, wid, week=2, day=5, description="")
    assert r["success"] and (r["after"]["week"], r["after"]["day"]) == (2, 5)
    assert tp.workouts[pid][wid]["workoutDay"] == "2026-11-27T00:00:00"
    r = await lo_plan_update_workout(pid, wid, structured_workout=_NATIVE)
    assert r["error_code"] == "VALIDATION_ERROR" and "tss_planned" in r["message"]
    r = await lo_plan_update_workout(pid, wid, week=2)
    assert r["error_code"] == "VALIDATION_ERROR"
    r = await lo_plan_update_workout(pid, 1, title="x")
    assert r["error_code"] == "NOT_FOUND"
    assert sum(1 for m, _, _ in tp.writes if m == "PUT") == 1


async def test_delete_dry_run_then_delete_reports_week_shift(tp):
    pid = tp.add_plan(workouts=[_w("2026-11-16", title="第一週"), _w("2026-11-25", title="第二週")])
    first = min(tp.workouts[pid])
    dry = await lo_plan_delete_workouts(pid, [first, 123], dry_run=True)
    assert [row["status"] for row in dry["rows"]] == ["would_delete", "not_found"] and tp.writes == []
    r = await lo_plan_delete_workouts(pid, [first])
    assert r["success"] and r["deleted"] == 1 and first not in tp.workouts[pid]
    assert "Week 1 moved" in r["warnings"][0] and r["plan"]["week1_monday"] == "2026-11-23"


async def test_published_plan_is_locked_for_every_write(tp):
    pid = tp.add_plan(title="12 週 113", is_public=True, workouts=[_w("2026-11-16", description="原文")])
    wid = next(iter(tp.workouts[pid]))
    item = [{"week": 1, "day": 2, "sport": "Run", "title": "新課"}]
    results = [
        await lo_plan_add_workouts(pid, item),
        await lo_plan_update_workout(pid, wid, title="改"),
        await lo_plan_delete_workouts(pid, [wid]),
        await lo_plan_update_meta(pid, description="改"),
    ]
    assert all(r["error_code"] == "PUBLIC_PLAN_LOCKED" for r in results) and tp.writes == []
    assert (await lo_plan_get_workouts(pid))["count"] == 1  # reading stays open

    r = await lo_plan_update_workout(pid, wid, title="改", allow_public=True)
    assert r["success"] and tp.workouts[pid][wid]["description"] == "原文"


async def test_update_meta_leaves_store_fields_alone(tp):
    pid = tp.add_plan(title="舊名")
    r = await lo_plan_update_meta(pid, title="新名", description="介紹")
    assert r["success"] and r["store_fields_untouched"] and set(r["changed"]) == {"title", "description"}
    body = tp.writes[0][2]
    assert body["isPublic"] is False and body["price"] is None and body["planId"] == pid
    same = await lo_plan_update_meta(pid, title="新名")
    assert same["changed"] == {} and len(tp.writes) == 1


async def test_get_workouts_filters_weeks_and_full_adds_structure(tp):
    pid = tp.add_plan(workouts=[
        _w("2026-11-16", 2, "騎", description="x" * 200, structure=dict(_NATIVE), totalTimePlanned=1.0),
        _w("2026-11-24", 3, "跑"),
    ])
    brief = await lo_plan_get_workouts(pid, weeks=[1])
    assert brief["count"] == 1 and brief["total_in_plan"] == 2
    row = brief["workouts"][0]
    assert row["has_structure"] and len(row["description_head"]) == 60 and "structured_workout" not in row
    full = await lo_plan_get_workouts(pid, full=True)
    assert full["workouts"][0]["structured_workout"] == _NATIVE and len(full["workouts"][0]["description"]) == 200


async def test_coach_toolset_and_metadata(monkeypatch):
    monkeypatch.setenv("TP_MCP_TOOLSET", "coach")
    tools = {t.name: t for t in await server.list_tools()}
    assert {"tp_list_training_plans", "tp_get_training_plan", "lo_plan_get_workouts", "lo_plan_create",
            "lo_plan_update_meta", "lo_plan_add_workouts", "lo_plan_update_workout",
            "lo_plan_delete_workouts"} <= set(tools)
    assert "tp_apply_training_plan" not in tools and "tp_get_training_plan_workouts" not in tools
    assert tools["lo_plan_get_workouts"].annotations.read_only_hint is True
    assert tools["lo_plan_delete_workouts"].annotations.destructive_hint is True
    assert tools["lo_plan_add_workouts"].annotations.idempotent_hint is False
    for name in ("lo_plan_add_workouts", "tp_list_training_plans"):
        assert "athlete" not in tools[name].input_schema["properties"]
    hidden = await server.call_tool("tp_apply_training_plan", {"plan_id": 1, "start_date": "2026-01-01"})
    out = json.loads(hidden[0].text)
    assert out["error_code"] == "HIDDEN_TOOL"
