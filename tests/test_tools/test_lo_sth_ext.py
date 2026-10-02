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
        self.frozen_duration = False
        self.reorder_409 = False

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
                        "durationMin": (25.0 if self.frozen_duration else round(r["cj"].get("durationSeconds", 1500) / 60, 1)),
                        "plannedSTL": r["cj"].get("stl", 0), "scheduleVersion": f"v{r['ver']}",
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
            if self.reorder_409:
                return {"ok": False, "error": "409001 课表正在同步设备"}
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
    fin = out["finish"]
    assert out["summary"].startswith("3/3") and fin["validate"]["ok"] and f.sleeps == []
    days = {r["date"]: r["order"] for r in fin["reorder"]}
    assert days["2026-10-08"][0] == coach_swim and len(days["2026-10-07"]) == 2
    assert fin["band"]["status"] in ("in", "under", "over") and fin["status"] == "done"
    ids = [r["classScheduleId"] for r in out["rows"]]
    assert all(ext.load_snapshot("T1", i) for i in ids)


# ---------- 10/02 優化 1：時長欄位回填＋週課表驗收 ----------
def test_update_replace_syncs_duration_seconds_and_checks_week_view(tmp_path):
    f = Fake()
    cid = f.add("2026-10-07", bike_cj())
    new = bike_cj(secs=900)
    new["durationSeconds"] = 999          # 教練介面改完常見：欄位沒跟著變
    new["duration"] = "00:16:39"
    p = tmp_path / "cj.json"
    p.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    out = run(ext.lo_sth_update_verified("T1", cid, classes_json_file=str(p), client_factory=lambda: f))
    assert out["ok"] and out["week_view"]["ok"] and out["duration_min"] == 25
    assert f.store[cid]["cj"]["durationSeconds"] == 1500 and f.store[cid]["cj"]["duration"] == "00:25:00"
    assert any(w["code"] == "DURATIONS_SYNCED" for w in out["warnings"])


def test_update_replace_fails_when_week_view_still_shows_old_duration(tmp_path):
    f = Fake()
    f.frozen_duration = True                # 週課表一直顯示 25 分
    cid = f.add("2026-10-07", bike_cj())
    p = tmp_path / "cj.json"
    p.write_text(json.dumps(bike_cj(secs=1800), ensure_ascii=False), encoding="utf-8")
    out = run(ext.lo_sth_update_verified("T1", cid, classes_json_file=str(p), client_factory=lambda: f))
    assert out["ok"] is False and out["week_view"]["ok"] is False


# ---------- 優化 3：官方負荷合理性 ----------
def test_load_unchanged_after_structure_change_warns_and_can_scale(tmp_path):
    f = Fake()
    old = bike_cj()
    old["stl"], old["sth"] = 84, 13800     # 官方 calculate 在 Fake 裡永遠回 84
    cid = f.add("2026-10-07", old)
    p = tmp_path / "cj.json"
    p.write_text(json.dumps(bike_cj(secs=1800), ensure_ascii=False), encoding="utf-8")
    out = run(ext.lo_sth_update_verified("T1", cid, classes_json_file=str(p), recalc_load=True, client_factory=lambda: f))
    w = next(x for x in out["warnings"] if x["code"] == "LOAD_UNCHANGED_AFTER_STRUCTURE_CHANGE")
    assert w["suggested_stl"] > 84 and out["stl"] == 84
    f.store[cid]["cj"] = old
    out2 = run(ext.lo_sth_update_verified("T1", cid, classes_json_file=str(p), recalc_load=True,
                                          load_fallback="scale", client_factory=lambda: f))
    assert out2["stl"] == w["suggested_stl"] and f.store[cid]["cj"]["stl"] == w["suggested_stl"]


# ---------- 優化 4：完整性 ----------
def _run_cj(secs_4k=600):
    return {"title": "節奏跑", "sportType": "RUN", "stl": 65, "durationSeconds": 2559, "duration": "00:52:39",
            "summary": "- 4公里@5:12~4:42/km", "trainingAdvice": "",
            "stages": [{"times": 1, "sections": [{"stageMode": "warmup", "capacity": "distance", "targetDistance": 4,
                                                   "targetSeconds": secs_4k, "thresholdSpeedRange": [77, 85],
                                                   "thresholdSpeedRangeNum": [312, 282]}]},
                       {"times": 1, "sections": [{"stageMode": "cooling", "capacity": "time", "targetSeconds": 300,
                                                   "thresholdSpeedRange": [65, 75], "thresholdSpeedRangeNum": [369, 320]}]}],
            "timeline": [{"duration": 600, "times": 1, "stageTimeline": [{"duration": 600}]},
                         {"duration": 300, "times": 1, "stageTimeline": [{"duration": 300}]}]}


def test_integrity_flags_implausible_distance_seconds_and_stale_fields():
    codes = {i["code"] for i in ext.integrity(_run_cj(), week_duration_min=42.6)}
    assert {"IMPLAUSIBLE_TARGET_SECONDS", "DURATION_SECONDS_STALE", "DURATION_STR_STALE"} <= codes
    fixed = ext.apply_integrity_fix(_run_cj())
    assert fixed["stages"][0]["sections"][0]["targetSeconds"] == 1188
    assert fixed["durationSeconds"] == 1488 and fixed["timeline"][0]["duration"] == 1188
    assert ext.integrity(fixed) == []


# ---------- 優化 4／5／6：diff_week ----------
def test_diff_week_text_diff_compact_structure_new_course_week_stl_and_fix_file(tmp_path):
    f = Fake()
    cid = f.add("2026-10-08", _run_cj(secs_4k=1188) | {"durationSeconds": 1488, "duration": "00:24:48"})
    ext.save_snapshot("T1", cid, copy.deepcopy(f.store[cid]["cj"]), "2026-10-08", "v1", "節奏跑")
    cj = f.store[cid]["cj"]
    cj["stages"][0]["sections"][0]["targetSeconds"] = 600      # 教練介面改壞
    cj["summary"] = "- 4公里@5:12~4:42/km\n- 伸展"
    f.store[cid]["ver"] = 2
    newc = f.add("2026-10-06", {"title": "輕鬆跑", "sportType": "RUN", "stl": 33, "summary": "- 輕鬆慢跑40分鐘",
                                "trainingAdvice": "鼻子呼吸"})
    out = run(ext.lo_sth_diff_week("T1", "2026-10-05", "2026-10-11", save_to=str(tmp_path / "d.json"),
                                   band=[90, 100], band_sports=["RUN"], client_factory=lambda: f))
    ch = out["changed"][0]
    assert ch["changes"]["summary"] == ["+- 伸展"]
    assert "structure" not in ch["changes"]   # 距離段只改秒數，結構指紋看不出來——所以才要 integrity
    assert any(i["code"] == "IMPLAUSIBLE_TARGET_SECONDS" for i in ch["integrity"])
    fixed = json.loads(open(ch["fix_file"], encoding="utf-8").read())
    assert fixed["stages"][0]["sections"][0]["targetSeconds"] == 1188
    assert out["new"][0]["id"] == newc and "輕鬆慢跑" in out["new"][0]["summary"]
    assert out["week_stl"]["RUN"] == 98 and out["band"]["status"] == "in"


# ---------- 優化 2：防重複＋收尾 ----------
def test_create_week_skips_existing_same_course_and_rerun_creates_nothing(tmp_path):
    f = Fake()
    dup = f.add("2026-10-07", bike_cj())                     # 平台上已經有同日同項目同標題「節奏騎」
    pf = _payload(tmp_path)
    out = run(lo_sth.lo_sth_create_week(pf, write=True, client_factory=lambda: f))
    st = {r["title"]: r["status"] for r in out["rows"]}
    assert st["節奏騎"] == "skipped_existing" and st["換項跑"] == "created"
    assert out["summary"].startswith("3/3")
    n_creates = sum(1 for t, a in f.calls if t == "create_workout" and a.get("dry_run") is False)
    again = run(lo_sth.lo_sth_create_week(pf, write=True, client_factory=lambda: f))
    assert all(r["status"] in ("already_created", "skipped_existing") for r in again["rows"])
    assert sum(1 for t, a in f.calls if t == "create_workout" and a.get("dry_run") is False) == n_creates
    p = json.loads(open(pf, encoding="utf-8").read())
    assert p["workouts"][0]["_classScheduleId"] == dup


def test_finish_week_returns_pending_when_device_keeps_syncing(tmp_path):
    f = Fake()
    pf = _payload(tmp_path)
    run(lo_sth.lo_sth_create_week(pf, write=True, client_factory=lambda: f))
    f.add("2026-10-07", {"title": "長訓", "sportType": "SWIM", "stl": 0})   # 教練事後加的課，應排最前
    f.reorder_409 = True

    async def nosleep(x):
        f.sleeps.append(x)
    out = run(lo_sth.lo_sth_finish_week(pf, client_factory=lambda: f, sleep=nosleep, time_budget_s=30, retry_wait_s=15))
    assert out["status"] == "pending_device_sync" and sum(f.sleeps) <= 30
    f.reorder_409 = False
    out2 = run(lo_sth.lo_sth_finish_week(pf, client_factory=lambda: f, sleep=nosleep))
    assert out2["status"] == "done" and out2["validate"]["ok"]


# ---------- 優化 7：逐段驗收＋紅旗 ----------
def test_intervals_reconstruct_two_threshold_blocks():
    from tp_mcp.tools import lo_sth_intervals as ivl
    cj = {"sportType": "CYCLE", "stages": [
        {"times": 1, "sections": [{"stageMode": "warmup", "capacity": "time", "targetSeconds": 600,
                                   "thresholdFtpRange": [50, 65], "thresholdFtpRangeNum": [105, 137]}]},
        {"times": 3, "sections": [{"stageMode": "bike", "capacity": "time", "targetSeconds": 30,
                                   "thresholdFtpRange": [118, 125], "thresholdFtpRangeNum": [248, 263]},
                                  {"stageMode": "bike", "capacity": "time", "targetSeconds": 90,
                                   "thresholdFtpRange": [50, 60], "thresholdFtpRangeNum": [105, 126]}]},
        {"times": 2, "sections": [{"stageMode": "bike", "capacity": "time", "targetSeconds": 900,
                                   "thresholdFtpRange": [95, 105], "thresholdFtpRangeNum": [200, 221]},
                                  {"stageMode": "recover", "capacity": "time", "targetSeconds": 300,
                                   "thresholdFtpRange": [50, 60], "thresholdFtpRangeNum": [105, 126]}]}]}
    pw = [120] * 960 + [205] * 900 + [115] * 300 + [212] * 900 + [110] * 300
    ts = [{"power": p, "speed": 30, "heartRate": 150} for p in pw]
    r = ivl.analyze(ts, cj)
    assert r["planned_blocks"] == 2 and r["match"] and [b["avg_w"] for b in r["blocks"]] == [205, 212]


def test_prep_week_red_flags_for_unplanned_long_ride(tmp_path):
    f = Fake()
    f.store[501] = {"date": "2026-09-29", "cj": bike_cj(), "ver": 3, "ref": "r501"}
    out = run(ext.lo_sth_prep_week("T1", "2026-10-05", str(tmp_path / "prep"), client_factory=lambda: f))
    assert any("公路骑行" in x for x in out["summary"]["red_flags"])


def test_calc_loads_writes_sidecar_with_signature(tmp_path):
    f = Fake()
    pf = _payload(tmp_path)
    out = run(lo_sth.lo_sth_calc_loads(pf, client_factory=lambda: f))
    side = json.loads(open(out["loads_sidecar"], encoding="utf-8").read())
    ent = side["2026-10-07|RIDE|節奏騎"]
    assert ent["stl"] == 84 and ent["sig"] == lo_sth.stages_sig(bike_cj())
