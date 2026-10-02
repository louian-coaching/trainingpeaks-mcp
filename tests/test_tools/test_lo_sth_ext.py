"""FORK 2026-10-02: lo_sth 提速 A/B/C/D — fake client, no network."""
import asyncio
import copy
import json

import pytest

from tp_mcp.tools import lo_sth, lo_sth_ext as ext


def run(c):
    return asyncio.run(c)


def bike_cj(w_lo=150, w_hi=162, pct=(88, 95), secs=720, text_w=None):
    tw = text_w or f"{w_lo}~{w_hi}W"
    return {"title": "節奏騎", "sportType": "CYCLE", "sth": 0, "stl": 0, "durationSeconds": 1500,
            "summary": f"- 熱身騎10分鐘@85~111W\n- 12分鐘@{tw}", "trainingAdvice": f"補給\n\n不准超過{w_hi}W。",
            "stages": [{"times": 1, "sections": [{"stageMode": "warmup", "capacity": "time", "targetSeconds": 600,
                                                   "thresholdFtpRange": [50, 65], "thresholdFtpRangeNum": [85, 111]}]},
                       {"times": 1, "sections": [{"stageMode": "bike", "capacity": "time", "targetSeconds": secs,
                                                   "thresholdFtpRange": list(pct), "thresholdFtpRangeNum": [w_lo, w_hi]}]}]}


class Fake:
    """記住每堂課的 classesJson 與版本；patch／update 真的改它，讀回會看到。"""

    def __init__(self, tri="T1"):
        self.tri = tri
        self.calls = []
        self.store = {}   # cid -> {"date","cj","ver","ref"}
        self.next = 99000
        self.sleeps = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    def add(self, date, cj):
        self.next += 1
        self.store[self.next] = {"date": date, "cj": copy.deepcopy(cj), "ver": 1, "ref": f"ref_{self.next}"}
        return self.next

    async def call(self, tool, a):
        self.calls.append((tool, a))
        if tool == "get_current_identity":
            return {"identityType": "C", "displayName": "羅譽寅"}
        if tool == "get_week_schedule":
            days = {}
            for cid, r in self.store.items():
                if a["date_from"] <= r["date"] <= a["date_to"]:
                    days.setdefault(r["date"], []).append({
                        "classScheduleId": cid, "title": r["cj"]["title"], "sportType": r["cj"]["sportType"],
                        "durationMin": 25.0, "plannedSTL": r["cj"].get("stl", 0), "scheduleVersion": f"v{r['ver']}",
                        "scheduleRef": r["ref"], "athleteRef": "aref", "displayOrder": len(days.get(r["date"], []))})
            return {"triUserId": a.get("tri_user_id"), "days": [{"date": d, "workouts": ws} for d, ws in sorted(days.items())]}
        if tool == "preview_assigned_workout_update":
            r = self.store[int(a["class_schedule_id"])]
            after = copy.deepcopy(r["cj"])
            after.update(json.loads(a.get("patch_json") or "{}"))
            return {"ok": True, "plan": {"afterClassesJson": json.dumps(after, ensure_ascii=False),
                                         "scheduleVersion": f"v{r['ver']}", "previewToken": "tok",
                                         "target": {"classesDate": r["date"] + " 00:00:00"}}}
        if tool == "update_assigned_workout_patch":
            r = self.store[int(a["class_schedule_id"])]
            assert a["confirm_token"] == ext.CONFIRM_PATCH and a["expected_version"] == f"v{r['ver']}"
            r["cj"].update(json.loads(a["patch_json"]))
            r["ver"] += 1
            return {"ok": True, "status": "ok"}
        if tool == "update_assigned_workout":
            cid = next(c for c, r in self.store.items() if r["ref"] == a["schedule_ref"])
            r = self.store[cid]
            if a.get("dry_run"):
                return {"ok": True, "blocking": [], "recommendedNextTool": {"args": {"expectedVersion": r["ver"]}}}
            assert a["expected_version"] == r["ver"] and a["confirm_token"] == ext.CONFIRM_UPDATE
            r["cj"] = json.loads(a["new_classes_json"])
            r["ver"] += 1
            return {"ok": True, "postWriteReadback": {"classesJsonMatches": True}}
        if tool == "calculate_workout_sth":
            return {"sth": 13800, "stl": 84}
        if tool == "list_recent_activities":
            return {"activities": [
                {"activityId": "A1", "sourceType": "device", "date": "2026-09-29", "sportType": "CYCLE", "title": "節奏騎8周",
                 "durationMin": 56, "avgPower": 132, "classScheduleId": 501, "completionPercent": 100},
                {"activityId": "A2", "sourceType": "device", "date": "2026-10-01", "sportType": "CYCLE", "title": "公路骑行",
                 "durationMin": 141, "avgPower": 83, "classScheduleId": None},
                {"activityId": "A3", "sourceType": "device", "date": "2026-09-08", "sportType": "SWIM", "title": "泳池"}]}
        if tool == "get_health_metrics":
            return {"deviceName": "coros", "daily": [{"date": "2026-09-30", "hrv": 72, "rhr": 55}, {"date": "2026-10-01"}]}
        if tool == "get_upcoming_races":
            return {"races": []}
        if tool == "get_activity_detail":
            return {"detail": {"activityName": "節奏騎", "isIndoor": True, "normalizedPower": 141,
                               "aerobicDecoupling": {"aerobicDecouplingPct": 3.9}},
                    "peakSummary": {"cleanPeakCurve": {"CYCLE": {"power": [{"window": "5m", "value": 167}, {"window": "5s", "value": 400}]}}}}
        if tool == "validate_schedule_week":
            return {"beginDay": a["date"], "endDay": a["date"], "scheduleCount": len(self.store), "statsConsistent": True,
                    "issues": [{"severity": "info", "code": "FUTURE_SCHEDULE"}]}
        if tool == "reorder_day":
            return {"ok": True, "orderApplied": True}
        if tool == "create_workout":
            if a.get("dry_run") is False:
                cid = self.add(a["classes_date"], json.loads(a["classes_json"]))
                return {"ok": True, "data": {"classScheduleId": cid}, "postCreateQualityGate": {"ok": True}, "readbackOk": True}
            return {"ok": True, "blocking": []}
        return {"ok": True}


@pytest.fixture(autouse=True)
def snapdir(tmp_path, monkeypatch):
    monkeypatch.setenv("LO_STH_SNAPSHOT_DIR", str(tmp_path / "snaps"))


# ---------- B ----------
def test_update_text_patch_verifies_and_snapshots(tmp_path):
    f = Fake()
    cid = f.add("2026-10-07", bike_cj())
    out = run(ext.lo_sth_update_verified("T1", cid, text={"summary": "- 新本體"}, client_factory=lambda: f))
    assert out["ok"] and out["fields_verified"] == ["summary"] and out["structure_untouched"]
    assert ext.load_snapshot("T1", cid)["classes_json"]["summary"] == "- 新本體"


def test_update_text_rejects_both_or_unknown_fields():
    f = Fake()
    cid = f.add("2026-10-07", bike_cj())
    assert run(ext.lo_sth_update_verified("T1", cid, client_factory=lambda: f))["error_code"] == "BAD_ARGS"
    assert run(ext.lo_sth_update_verified("T1", cid, text={"stl": 3}, client_factory=lambda: f))["error_code"] == "BAD_FIELDS"


def test_update_replace_finds_ref_dry_runs_version_and_recalcs(tmp_path):
    f = Fake()
    cid = f.add("2026-10-07", bike_cj())
    new = bike_cj(secs=900)
    p = tmp_path / "cj.json"
    p.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    out = run(ext.lo_sth_update_verified("T1", cid, classes_json_file=str(p), recalc_load=True, client_factory=lambda: f))
    assert out["ok"] and out["stl"] == 84 and out["structure_verified"] and out["text_verified"]
    assert f.store[cid]["cj"]["stages"][1]["sections"][0]["targetSeconds"] == 900
    kinds = [t for t, _ in f.calls]
    assert kinds.count("update_assigned_workout") == 2 and "calculate_workout_sth" in kinds


# ---------- D ----------
def test_diff_week_flags_stale_watts_after_coach_edit():
    f = Fake()
    cid = f.add("2026-10-07", bike_cj())
    ext.save_snapshot("T1", cid, copy.deepcopy(f.store[cid]["cj"]), "2026-10-07", "v1", "節奏騎")
    # 教練在圖形改：12 分鐘 → 15 分鐘、88-95 → 95-102（文字沒動）
    s = f.store[cid]["cj"]["stages"][1]["sections"][0]
    s.update({"targetSeconds": 900, "thresholdFtpRange": [95, 102], "thresholdFtpRangeNum": [162, 173]})
    f.store[cid]["ver"] = 2
    out = run(ext.lo_sth_diff_week("T1", "2026-10-05", "2026-10-11", client_factory=lambda: f))
    ch = out["changed"][0]
    assert "structure" in ch["changes"] and not ch["text_only"]
    stale = {(x["kind"], x["old"], x["new"]) for x in ch["stale_text"] if x["old_value_still_in_text"]}
    assert ("power", "150~162W", "162~173W") in stale and ("dur", "12分鐘", "15分鐘") in stale
    assert out["stale_total"] >= 2


def test_diff_week_moved_deleted_unchanged_and_accept():
    f = Fake()
    a = f.add("2026-10-08", bike_cj())
    b = f.add("2026-10-09", bike_cj())
    for cid in (a, b):
        ext.save_snapshot("T1", cid, copy.deepcopy(f.store[cid]["cj"]), f.store[cid]["date"], "v1", "x")
    f.store[a]["date"] = "2026-10-10"          # 教練移日期
    ghost = 98765
    ext.save_snapshot("T1", ghost, bike_cj(), "2026-10-06", "v1", "被刪的")
    out = run(ext.lo_sth_diff_week("T1", "2026-10-05", "2026-10-11", accept=True, client_factory=lambda: f))
    assert [c["changes"]["moved"] for c in out["changed"]] == [["2026-10-08", "2026-10-10"]]
    assert [d["id"] for d in out["deleted"]] == [ghost]
    again = run(ext.lo_sth_diff_week("T1", "2026-10-05", "2026-10-11", client_factory=lambda: f))
    assert again["changed"] == []


def test_stale_tokens_removed_section_and_pace():
    old = {"summary": "- 400公尺@3:53~3:43/km\n- 緩騎5分鐘", "trainingAdvice": "",
           "stages": [{"times": 8, "sections": [{"capacity": "distance", "targetDistance": 0.4,
                                                 "thresholdSpeedRange": [105, 110], "thresholdSpeedRangeNum": [233, 223]}]},
                      {"times": 1, "sections": [{"capacity": "time", "targetSeconds": 300,
                                                 "thresholdFtpRange": [50, 60], "thresholdFtpRangeNum": [105, 125]}]}]}
    new = copy.deepcopy(old)
    new["stages"][0]["sections"][0].update({"thresholdSpeedRange": [110, 120], "thresholdSpeedRangeNum": [223, 204]})
    new["stages"].pop()
    st = ext.stale_tokens(old, new)
    assert any(x["kind"] == "pace" and x["old"] == "3:53~3:43" and x["new"] == "3:43~3:24" for x in st)
    assert any(x["removed"] and x["kind"] == "dur" and x["old"] == "5分鐘" for x in st)


# ---------- A ----------
def test_prep_week_digest(tmp_path):
    f = Fake()
    f.store[501] = {"date": "2026-09-29", "cj": bike_cj(), "ver": 3, "ref": "r501"}
    f.store[502] = {"date": "2026-09-30", "cj": {**bike_cj(), "title": "輕鬆跑", "sportType": "RUN"}, "ver": 1, "ref": "r502"}
    f.store[600] = {"date": "2026-10-06", "cj": {**bike_cj(), "title": "長訓", "sportType": "SWIM"}, "ver": 1, "ref": "r600"}
    out = run(ext.lo_sth_prep_week("T1", "2026-10-05", str(tmp_path / "prep"), client_factory=lambda: f))
    s = out["summary"]
    assert s["done"] == 1 and s["last_swim"] == "2026-09-08" and s["quality_detail_count"] == 1
    assert any("公路骑行" in u for u in s["unplanned"]) and s["templates"] == 2
    assert s["next_week_existing"] == ["2026-10-06 長訓"]
    d = json.loads((tmp_path / "prep" / "prep.json").read_text(encoding="utf-8"))
    assert d["quality_details"]["501"]["peaks"]["power"] == {"5m": 167}
    assert ext.load_snapshot("T1", 501)["scheduleVersion"] == "v3"


def test_prep_week_stops_on_tri_mismatch(tmp_path):
    f = Fake()

    async def bad(tool, a, _orig=f.call):
        r = await _orig(tool, a)
        if tool == "get_week_schedule":
            r["triUserId"] = "COACH"
        return r
    f.call = bad
    out = run(ext.lo_sth_prep_week("T1", "2026-10-05", str(tmp_path / "p"), client_factory=lambda: f))
    assert out["error_code"] == "TRI_MISMATCH"


# ---------- C ----------
def _payload(tmp_path):
    p = {"tri_user_id": "T1", "workouts": [
        {"classes_date": "2026-10-07", "sport_type": "RIDE", "title": "節奏騎", "duration_min": 25, "classes_json": bike_cj()},
        {"classes_date": "2026-10-07", "sport_type": "RUN", "title": "換項跑", "duration_min": 10,
         "classes_json": {**bike_cj(), "title": "換項跑", "sportType": "RUN"}},
        {"classes_date": "2026-10-08", "sport_type": "SWIM", "title": "有氧耐力", "duration_min": 50,
         "classes_json": {"title": "有氧耐力", "sportType": "SWIM", "sth": 8250, "stl": 50}},
    ]}
    f = tmp_path / "week.json"
    f.write_text(json.dumps(p, ensure_ascii=False), encoding="utf-8")
    return str(f)


def test_calc_loads_band_and_per_min(tmp_path):
    f = Fake()
    pf = _payload(tmp_path)
    out = run(lo_sth.lo_sth_calc_loads(pf, client_factory=lambda: f, band=[150, 170], band_sports=["RUN", "RIDE"]))
    assert out["totals_by_sport"] == {"RIDE": 84, "RUN": 84, "SWIM": 50}
    assert out["band"]["status"] == "in" and out["band"]["total"] == 168
    assert out["rows"][0]["stl_per_min"] == round(84 / 25, 2)


def test_create_week_finish_reorders_validates_and_snapshots(tmp_path):
    f = Fake()
    coach_swim = f.add("2026-10-08", {"title": "長訓", "sportType": "SWIM", "stl": 0})
    pf = _payload(tmp_path)

    async def nosleep(s):
        f.sleeps.append(s)
    out = run(lo_sth.lo_sth_create_week(pf, write=True, finish=True, client_factory=lambda: f, sleep=nosleep,
                                        band=[100, 400], band_sports=["RUN", "RIDE", "SWIM"]))
    assert out["summary"] == "3/3 堂寫入" and out["validate"]["ok"] and f.sleeps == [45]
    days = {r["date"]: r["order"] for r in out["reorder"]}
    assert days["2026-10-08"][0] == coach_swim and len(days["2026-10-07"]) == 2
    assert out["band"]["status"] in ("in", "under", "over")
    ids = [r["classScheduleId"] for r in out["rows"]]
    assert all(ext.load_snapshot("T1", i) for i in ids)
