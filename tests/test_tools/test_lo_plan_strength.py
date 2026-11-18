"""FORK 2026-11-18: Strength Builder workouts inside a training plan (lo_plan_strength.py).

The fake strength API reproduces what was read live on 2026-11-18: a plan's strength
workout is a strength document whose calendarId is the plan's planPersonId, the plan
route lists summaries as a bare array, and save answers with a numeric id.
"""

import copy
import json
import re
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from tp_mcp import server
from tp_mcp.client.http import APIResponse
from tp_mcp.tools.lo_plan_strength import (
    lo_plan_add_strength_workouts,
    lo_plan_get_strength_workouts,
)

from .test_lo_plans import FakeTP, _w

CAT_COW, PLANK, SIDE_PLANK, GLUTE_BRIDGE = "475", "571", "79", "579"


def _core(week=1, day=2, title="核心訓練", **extra):
    return {"week": week, "day": day, "title": title, "instructions": "熱身不要跳過。", "blocks": [
        {"type": "WarmUp", "title": "熱身", "exercises": [{"id": CAT_COW, "notes": "貓牛式", "sets": [{"Reps": 10}]}]},
        {"type": "SingleExercise", "exercises": [
            {"id": PLANK, "notes": "平板式：身體成一直線。", "sets": [{"Duration": 30}] * 3}]},
        {"type": "SingleExercise", "exercises": [{"id": GLUTE_BRIDGE, "sets": [{"Reps": 12}] * 3}]},
    ], **extra}


class FakeStrength:
    def __init__(self, tp):
        self.tp = tp
        self.docs = {}
        self.posted = []
        self._seq = 4_600_000
        self.reject = set()
        self.lose_answer_for = set()
        self.drop_a_set_for = set()

    def _resp(self, status, data):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = data
        r.text = str(data)
        return r

    def seed(self, plan_id, day, title):
        self._seq += 1
        self.docs[str(self._seq)] = {
            "id": str(self._seq), "calendarId": self.tp.plans[plan_id]["planPersonId"],
            "prescribedDate": day, "title": title, "instructions": None, "blocks": [],
            "snapshot": {"totalBlocks": 0, "totalSets": 0}}
        return str(self._seq)

    async def get(self, url, headers=None):
        m = re.search(r"/rx/activity/v1/plans/(\d+)/workouts/(\d{4}-\d\d-\d\d)/(\d{4}-\d\d-\d\d)$", url)
        if m:
            person = self.tp.plans[int(m.group(1))]["planPersonId"]
            rows = [{"id": d["id"], "prescribedDate": d["prescribedDate"], "calendarId": d["calendarId"],
                     "title": d["title"], "instructions": d.get("instructions"),
                     "totalBlocks": d["snapshot"]["totalBlocks"], "totalSets": d["snapshot"]["totalSets"]}
                    for d in self.docs.values()
                    if d["calendarId"] == person and m.group(2) <= d["prescribedDate"] <= m.group(3)]
            return self._resp(200, rows)
        m = re.search(r"/rx/activity/v1/workouts/(\d+)$", url)
        assert m, url
        doc = self.docs.get(m.group(1))
        return self._resp(200, {"data": copy.deepcopy(doc)}) if doc else self._resp(404, {})

    async def post(self, url, headers=None, json=None):
        assert url.endswith("/rx/activity/v1/workouts/save"), url
        self.posted.append(copy.deepcopy(json))
        if json["title"] in self.reject:
            return self._resp(400, {"errors": {"blocks": ["bad"]}})
        self._seq += 1
        doc = copy.deepcopy(json)
        doc["id"] = str(self._seq)
        if json["title"] in self.drop_a_set_for:
            doc["blocks"][1]["prescriptions"][0]["sets"].pop()
        self.docs[doc["id"]] = doc
        if json["title"] in self.lose_answer_for:
            raise httpx.ReadTimeout("timeout")
        return self._resp(200, {"data": {"id": int(doc["id"])}})


@pytest.fixture
def env():
    tp = FakeTP()
    tp._ensure_access_token = AsyncMock(return_value=APIResponse(success=True))
    tp._token_cache = MagicMock(access_token="token")
    api = FakeStrength(tp)
    with patch("tp_mcp.tools.lo_plan_strength.TPClient") as cls, \
            patch("tp_mcp.tools.lo_plan_strength.httpx.AsyncClient") as http:
        cls.return_value.__aenter__.return_value = tp
        http.return_value.__aenter__.return_value = api
        yield tp, api


async def test_add_lands_on_the_plan_person_at_week_and_day(env):
    tp, api = env
    pid = tp.add_plan(workouts=[_w("2026-11-18")])  # a Wednesday: week 1 starts Mon 2026-11-16
    r = await lo_plan_add_strength_workouts(pid, [_core(week=1, day=2), _core(week=3, day=7, title="核心訓練 B")])
    assert r["success"] and r["created"] == 2 and all(row["verified"] for row in r["rows"])
    first, second = api.posted
    assert first["calendarId"] == tp.plans[pid]["planPersonId"] and first["workoutType"] == "StructuredStrength"
    assert first["prescribedDate"] == "2026-11-17" and second["prescribedDate"] == "2026-12-06"
    assert first["blocks"][0]["blockType"] == "WarmUp" and first["snapshot"]["totalSets"] == 7
    assert first["blocks"][1]["prescriptions"][0]["coachNotes"] == "平板式：身體成一直線。"
    assert [row["workout_id"] for row in r["rows"]] == list(api.docs)
    assert tp.writes == []  # nothing goes through /plans/v1


async def test_get_lists_by_week_and_full_round_trips_as_a_payload(env, tmp_path):
    tp, _ = env
    pid = tp.add_plan(workouts=[_w("2026-11-16")])
    await lo_plan_add_strength_workouts(pid, [_core(week=2, day=2), _core(week=1, day=2)])
    brief = await lo_plan_get_strength_workouts(pid)
    assert [(w["week"], w["day"], w["total_sets"]) for w in brief["workouts"]] == [(1, 2, 7), (2, 2, 7)]
    assert brief["workouts"][0]["instructions_head"] == "熱身不要跳過。" and "blocks" not in brief["workouts"][0]
    only = await lo_plan_get_strength_workouts(pid, weeks=[2], full=True)
    assert only["count"] == 1
    got = only["workouts"][0]["blocks"]
    assert got[1] == {"type": "SingleExercise", "title": None, "notes": None, "exercises": [
        {"id": PLANK, "exercise": "Forearm Plank", "notes": "平板式：身體成一直線。",
         "sets": [{"Duration": "30"}] * 3}]}
    target = tmp_path / "strength.json"
    saved = await lo_plan_get_strength_workouts(pid, save_to=str(target))
    assert saved["saved_to"] == str(target) and "blocks" not in saved["workouts"][0]
    rows = json.loads(target.read_text(encoding="utf-8"))["workouts"]
    # a read-back row is itself a valid payload item for another plan
    other = tp.add_plan(workouts=[_w("2027-01-04")])
    again = await lo_plan_add_strength_workouts(other, [
        {k: row[k] for k in ("week", "day", "title", "instructions", "blocks")} for row in rows])
    assert again["success"] and again["created"] == 2


async def test_skip_if_exists_and_dry_run_write_nothing(env):
    tp, api = env
    pid = tp.add_plan(workouts=[_w("2026-11-16")])
    api.seed(pid, "2026-11-17", "核心訓練")
    dry = await lo_plan_add_strength_workouts(pid, [_core(), _core(week=2)], dry_run=True)
    assert dry["dry_run"] and dry["would_create"] == 1 and dry["skipped"] == 1 and api.posted == []
    assert dry["rows"][1]["exercises"] == ["Cat Cow (475)", "Forearm Plank (571)", "Glute Bridge (579)"]
    r = await lo_plan_add_strength_workouts(pid, [_core(), _core(week=2)])
    assert [row["status"] for row in r["rows"]] == ["skipped_exists", "created"] and len(api.posted) == 1
    twice = await lo_plan_add_strength_workouts(pid, [_core()], skip_if_exists=False)
    assert twice["created"] == 1


async def test_lost_answer_is_found_in_the_list_not_resent(env):
    tp, api = env
    pid = tp.add_plan(workouts=[_w("2026-11-16")])
    api.lose_answer_for.add("核心訓練")
    r = await lo_plan_add_strength_workouts(pid, [_core()])
    assert r["success"] and r["rows"][0]["status"] == "created" and r["rows"][0]["verified"]
    assert len(api.posted) == 1 and "error" not in r["rows"][0]


async def test_rejected_and_mismatched_writes_are_reported(env):
    tp, api = env
    pid = tp.add_plan(workouts=[_w("2026-11-16")])
    api.reject.add("壞的")
    api.drop_a_set_for.add("少一組")
    r = await lo_plan_add_strength_workouts(pid, [_core(title="壞的"), _core(week=2, title="少一組")])
    assert r["success"] is False and r["problems"] == 1
    bad, short = r["rows"]
    assert bad["status"] == "failed" and "blocks" in bad["error"]
    assert short["status"] == "created" and short["verified"] is False and "sets expected" in short["mismatch"][0]


async def test_validation_and_lint_stop_before_any_request(env):
    tp, api = env
    pid = tp.add_plan(workouts=[_w("2026-11-16")])
    no_warmup = _core()
    no_warmup["blocks"] = no_warmup["blocks"][1:]
    r = await lo_plan_add_strength_workouts(pid, [_core(), no_warmup | {"week": 2}])
    assert r["error_code"] == "LINT_FAILED" and r["errors"][0]["index"] == 1 and api.posted == []
    forced = await lo_plan_add_strength_workouts(pid, [no_warmup], force=True)
    assert forced["success"] and "forced_past" in forced["rows"][0]
    mobility = await lo_plan_add_strength_workouts(pid, [no_warmup | {"week": 3, "mobility": True}])
    assert mobility["success"] and "forced_past" not in mobility["rows"][0]
    for broken in ({"week": 0, "day": 1, "title": "x", "blocks": []}, _core(day=8), _core(title=" "),
                   _core() | {"sport": "Strength"},
                   _core() | {"blocks": [{"type": "WarmUp", "exercises": [{"id": "0", "sets": [{"Reps": 1}]}]}]}):
        r = await lo_plan_add_strength_workouts(pid, [broken])
        assert r["error_code"] == "VALIDATION_ERROR", broken


async def test_loaded_exercise_without_weight_only_warns_in_a_plan(env):
    tp, _ = env
    pid = tp.add_plan(workouts=[_w("2026-11-16")])
    item = _core()
    from tp_mcp.tools.strength import _LOADED_RE, _catalogue
    loaded = next(i for i, e in _catalogue().items() if _LOADED_RE.search(e["title"]))
    item["blocks"].append({"type": "SingleExercise", "exercises": [{"id": loaded, "sets": [{"Reps": 10}] * 3}]})
    r = await lo_plan_add_strength_workouts(pid, [item])
    assert r["success"] and "no weight" in r["rows"][0]["lint_warnings"][-1]


async def test_empty_plan_published_plan_and_payload_file(env, tmp_path):
    tp, api = env
    empty = tp.add_plan()
    r = await lo_plan_add_strength_workouts(empty, [_core()])
    assert r["error_code"] == "VALIDATION_ERROR" and "empty" in r["message"]
    assert (await lo_plan_get_strength_workouts(empty))["count"] == 0
    public = tp.add_plan(is_public=True, workouts=[_w("2026-11-16")])
    locked = await lo_plan_add_strength_workouts(public, [_core()])
    assert locked["error_code"] == "PUBLIC_PLAN_LOCKED" and api.posted == []
    f = tmp_path / "strength.json"
    f.write_text(json.dumps({"workouts": [_core()]}, ensure_ascii=False), encoding="utf-8")
    both = await lo_plan_add_strength_workouts(public, [_core()], payload_file=str(f))
    assert both["error_code"] == "VALIDATION_ERROR"
    ok = await lo_plan_add_strength_workouts(public, payload_file=str(f), allow_public=True)
    assert ok["success"] and ok["created"] == 1
    too_many = await lo_plan_add_strength_workouts(public, [_core(title=f"t{i}") for i in range(41)])
    assert too_many["error_code"] == "VALIDATION_ERROR" and "41" in too_many["message"]


async def test_registered_for_the_coach_toolset(monkeypatch, env):
    monkeypatch.setenv("TP_MCP_TOOLSET", "coach")
    tools = {t.name: t for t in await server.list_tools()}
    get, add = tools["lo_plan_get_strength_workouts"], tools["lo_plan_add_strength_workouts"]
    assert get.annotations.read_only_hint is True and add.annotations.read_only_hint is False
    assert add.annotations.idempotent_hint is False and add.annotations.destructive_hint is False
    assert "athlete" not in get.input_schema["properties"] and "athlete" not in add.input_schema["properties"]
    tp, _ = env
    pid = tp.add_plan(workouts=[_w("2026-11-16")])
    out = await server.call_tool("lo_plan_add_strength_workouts", {"plan_id": pid, "workouts": [_core()]})
    assert json.loads(out[0].text)["success"] is True
