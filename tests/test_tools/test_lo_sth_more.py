"""FORK 2026-11-16: StrongTri 優化七項（①②③④⑤）— fake client, no network."""

import asyncio
import copy
import json
from pathlib import Path

from tests.test_tools.test_lo_sth_ext import Fake, bike_cj
from tp_mcp.tools import lo_sth
from tp_mcp.tools import lo_sth_ext as ext
from tp_mcp.tools import lo_sth_more as more


def run(c):
    return asyncio.run(c)


def run_cj(km=10, secs=3600, pct=(80, 85), text="- 10公里@6:20~5:58/km"):
    return {
        "title": "有氧耐力",
        "sportType": "RUN",
        "sth": 0,
        "stl": 60,
        "durationSeconds": secs + 300,
        "summary": f"- 暖身跑5分鐘\n{text}",
        "trainingAdvice": "補水\n\n配速守在6:20~5:58/km。",
        "links": [],
        "stages": [
            {
                "times": 1,
                "sections": [
                    {
                        "stageMode": "warmup",
                        "capacity": "time",
                        "targetSeconds": 300,
                        "thresholdSpeedRange": [60, 65],
                        "title": "热身",
                    }
                ],
            },
            {
                "times": 1,
                "sections": [
                    {
                        "stageMode": "run",
                        "capacity": "distance",
                        "targetDistance": km,
                        "targetSeconds": secs,
                        "thresholdSpeedRange": list(pct),
                        "title": "跑步",
                    }
                ],
            },
        ],
    }


class ThrFake(Fake):
    """加上閾值讀寫：apply_threshold_update 會像平台一樣重算瓦數／距離段秒數（PLAT-19）、不動文字（PLAT-21）。"""

    def __init__(self, ftp=200.0, pace=300.0, land=True, flag_ok=True):
        super().__init__()
        self.thr = {"heart_rate": 168.0, "power": ftp, "run_pace": pace, "swim_pace": 133.0}
        self.land, self.flag_ok = land, flag_ok
        self.patch_flag_ok = True
        self.patch_writes = True

    async def call(self, tool, a):
        if tool == "get_athlete_thresholds":
            self.calls.append((tool, a))
            return {
                "triUserId": a["tri_user_id"],
                "thresholds": {
                    k: {"threshold": f"{v:.2f}", "zones": {"zone2": ["1", "2"]}} for k, v in self.thr.items()
                },
            }
        if tool == "apply_threshold_update":
            self.calls.append((tool, a))
            if a.get("dry_run"):
                return {"ok": True, "deviceSyncImpact": "recompute_future_run_cycle_schedules_and_resync"}
            assert a["confirm_token"] == more.CONFIRM_THRESHOLD and a["confirm_write"] is True
            key = {2: "power", 3: "run_pace"}[a["threshold_type"]]
            old = self.thr[key]
            if self.land:
                self.thr[key] = float(a["threshold"])
                for r in self.store.values():
                    for st in r["cj"].get("stages") or []:
                        for s in st["sections"]:
                            if key == "run_pace" and s.get("capacity") == "distance":
                                s["targetSeconds"] = int(round(s["targetSeconds"] * a["threshold"] / old))
                    r["ver"] += 1
            return {"ok": self.flag_ok}
        if tool == "update_assigned_workout_patch":
            self.calls.append((tool, a))
            r = self.store[int(a["class_schedule_id"])]
            if self.patch_writes:
                r["cj"].update(json.loads(a["patch_json"]))
                r["ver"] += 1
            return {"ok": self.patch_flag_ok}
        res = await super().call(tool, a)
        if tool == "get_week_schedule":
            res["athleteRef"] = "aref"
        return res


# ---------- ⑤ 誤報失敗 ----------
def test_update_text_false_negative_flag_is_read_through():
    f = ThrFake()
    f.patch_flag_ok = False
    cid = f.add("2026-11-18", bike_cj())
    out = run(ext.lo_sth_update_verified("T1", cid, text={"trainingAdvice": "新說明"}, client_factory=lambda: f))
    assert out["ok"] and not out.get("isError")
    assert out["warnings"][0]["code"] == "PLATFORM_FLAG_FALSE_NEGATIVE"


def test_update_text_real_failure_still_fails():
    f = ThrFake()
    f.patch_flag_ok, f.patch_writes = False, False
    cid = f.add("2026-11-18", bike_cj())
    out = run(ext.lo_sth_update_verified("T1", cid, text={"trainingAdvice": "新說明"}, client_factory=lambda: f))
    assert out["isError"] and out["error_code"] == "WRITE_FAILED" and out["mismatch"] == ["trainingAdvice"]


# ---------- ④ 待辦 ----------
def _payload(tmp_path, tri="T1"):
    rows = [
        {
            "classes_date": "2026-11-19",
            "sport_type": "RUN",
            "title": "跑",
            "duration_min": 40,
            "classes_json": {**bike_cj(), "title": "跑", "sportType": "RUN"},
        },
        {
            "classes_date": "2026-11-19",
            "sport_type": "SWIM",
            "title": "游",
            "duration_min": 50,
            "classes_json": {"title": "游", "sportType": "SWIM", "summary": "x", "stl": 50},
        },
    ]
    p = tmp_path / "week.json"
    p.write_text(json.dumps({"tri_user_id": tri, "workouts": rows}, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_finish_week_queues_pending_and_pending_tool_clears_it(tmp_path):
    f = ThrFake()
    pf = _payload(tmp_path)
    run(lo_sth.lo_sth_create_week(pf, write=True, client_factory=lambda: f))
    f.add("2026-11-19", {"title": "長訓", "sportType": "SWIM", "stl": 0})  # 教練事後加的課 → 要重排
    f.reorder_409 = True

    async def nosleep(x):
        return None

    out = run(lo_sth.lo_sth_finish_week(pf, client_factory=lambda: f, sleep=nosleep, time_budget_s=0))
    assert out["status"] == "pending_device_sync" and out["queued"]
    q = more.list_pending("T1")["T1"]
    assert q[0]["payload_file"] == pf and q[0]["days"] == ["2026-11-19"]
    # 還在同步：留在佇列
    r1 = run(more.lo_sth_pending("T1", time_budget_s=0, client_factory=lambda: f))
    assert r1["remaining_count"] == 1 and r1["rows"][0]["status"] == "still_pending"
    f.reorder_409 = False
    r2 = run(more.lo_sth_pending(client_factory=lambda: f))
    assert r2["rows"][0]["status"] == "done" and r2["remaining_count"] == 0


def test_prep_week_retries_pending_without_waiting(tmp_path):
    f = ThrFake()
    pf = _payload(tmp_path)
    run(lo_sth.lo_sth_create_week(pf, write=True, client_factory=lambda: f))
    more.add_pending("T1", pf, ["2026-11-19"])
    out = run(
        ext.lo_sth_prep_week("T1", "2026-11-23", str(tmp_path / "prep"), client_factory=lambda: f, intervals=False)
    )
    assert out["summary"]["pending_reorder"][0]["status"] == "done"
    assert more.list_pending("T1") == {}


def test_finish_week_done_clears_stale_queue(tmp_path):
    f = ThrFake()
    pf = _payload(tmp_path)
    run(lo_sth.lo_sth_create_week(pf, write=True, client_factory=lambda: f))
    more.add_pending("T1", pf, ["2026-11-19"])
    out = run(lo_sth.lo_sth_finish_week(pf, client_factory=lambda: f))
    assert out["status"] == "done" and more.list_pending("T1") == {}


# ---------- ② 文字草稿 ----------
def test_diff_drafts_follow_graph_edit_in_summary_and_advice(tmp_path):
    f = ThrFake()
    cid = f.add("2026-11-18", bike_cj())
    ext.save_snapshot("T1", cid, copy.deepcopy(f.store[cid]["cj"]), "2026-11-18", "v1", "節奏騎")
    s = f.store[cid]["cj"]["stages"][1]["sections"][0]
    s.update({"targetSeconds": 900, "thresholdFtpRange": [95, 102], "thresholdFtpRangeNum": [162, 173]})
    f.store[cid]["ver"] = 2
    out = run(
        ext.lo_sth_diff_week(
            "T1", "2026-11-16", "2026-11-22", save_to=str(tmp_path / "diff.json"), client_factory=lambda: f
        )
    )
    ch = out["changed"][0]
    d = ch["explanation_drafts"]
    assert "- 15分鐘@162~173W" in d["text_patch"]["summary"]
    assert "trainingAdvice" not in d["text_patch"]  # 說明段只有單值 162W（不是區間）→ 不亂換、留人工
    assert out["drafts_total"] == 1 and json.loads(Path(ch["draft_file"]).read_text(encoding="utf-8"))["summary"]


def test_diff_drafts_use_coach_summary_and_flag_ambiguous_duration():
    old = bike_cj()
    old["trainingAdvice"] = "主課12分鐘守住150~162W，最後12分鐘別衝。"
    new = copy.deepcopy(old)
    new["summary"] = "- 熱身騎10分鐘@85~111W\n- 15分鐘@155~168W"
    d = more.sth_text_drafts(old, new, [], "2026-11-18", "2026-11-18")
    adv = d["fields"]["trainingAdvice"]
    assert "155~168W" in adv["draft"] and "12分鐘" in adv["unresolved"]
    assert "summary" not in d["text_patch"] and d["coach_edited_summary"]


def test_diff_drafts_shift_post_race_day_on_move():
    old = bike_cj()
    old["trainingAdvice"] = "比賽後第2天，輕鬆動一動。"
    d = more.sth_text_drafts(old, copy.deepcopy(old), [], "2026-11-17", "2026-11-18")
    assert d["text_patch"]["trainingAdvice"] == "比賽後第3天，輕鬆動一動。"


# ---------- ① validate 轉接 ----------
def test_to_validate_workout_maps_stages_links_and_cadence():
    cj = bike_cj()
    cj["stages"][1]["sections"][0].update({"hasCadence": True, "cadence": [85, 95]})
    cj["stages"].append(
        {
            "times": 3,
            "sections": [
                {"stageMode": "recover", "capacity": "time", "targetSeconds": 60, "thresholdFtpRange": [50, 55]}
            ],
        }
    )
    cj["links"] = [{"title": "x", "type": "1", "url": "https://shimo.im/a"}]
    w = {"classScheduleId": 7, "title": "節奏騎", "sportType": "CYCLE", "plannedDurationSec": 1500, "plannedSTL": 40}
    v = more.to_validate_workout("2026-11-18", w, cj)
    assert v["sport"] == "Bike" and v["tss_planned"] == 40 and v["duration_planned"] == round(1500 / 3600, 3)
    assert "－－" in v["description"] and v["description"].endswith("https://shimo.im/a")
    sw = v["structured_workout"]
    assert (
        sw["primaryIntensityMetric"] == "percentOfFtp" and sw["structure"][0]["steps"][0]["intensityClass"] == "warmUp"
    )
    tg = sw["structure"][1]["steps"][0]["targets"]
    assert tg[1]["unit"] == "roundOrStridePerMinute" and tg[1]["minValue"] == 85
    assert sw["structure"][2]["type"] == "repetition" and sw["structure"][2]["steps"][0]["intensityClass"] == "rest"
    r = more.to_validate_workout("2026-11-19", {"classScheduleId": 8, "sportType": "RUN"}, run_cj())
    assert r["structured_workout"]["primaryLengthMetric"] == "distance"
    assert r["structured_workout"]["structure"][1]["steps"][0]["length"] == {"value": 10000, "unit": "meter"}


def test_get_week_for_validate_saves_platform_file(tmp_path):
    f = ThrFake()
    f.add("2026-11-18", bike_cj())
    f.add("2026-11-20", {"title": "肌力訓練", "sportType": "STRENGTH", "summary": "深蹲 10下 3組", "stl": 20})
    out = run(
        more.lo_sth_get_week_for_validate(
            "T1", "2026-11-16", "2026-11-22", str(tmp_path / "v.json"), client_factory=lambda: f
        )
    )
    data = json.loads(Path(out["saved_to"]).read_text(encoding="utf-8"))
    assert data["platform"] == "strongtri" and len(data["workouts"]) == 2
    assert "--strength-days 2026-11-20" in out["suggested_flags"]


# ---------- ③ 改閾值 ----------
def test_threshold_ftp_writes_verifies_and_previews_text():
    f = ThrFake(ftp=200.0)
    cid = f.add("2026-11-18", bike_cj())
    ext.save_snapshot("T1", cid, copy.deepcopy(f.store[cid]["cj"]), "2026-11-18", "v1", "節奏騎")
    out = run(more.lo_sth_update_threshold_verified("T1", "ftp", 210, today="2026-11-16", client_factory=lambda: f))
    assert out["ok"] and out["landed"] == 210.0 and out["old"] == 200.0
    pv = out["text"]["preview"][0]
    assert "158~170W" in pv["patch"]["summary"] and "不准超過170W" in pv["patch"]["trainingAdvice"]
    assert f.store[cid]["cj"]["summary"] == bike_cj()["summary"]  # preview 不寫
    assert ext.load_snapshot("T1", cid)["source"] == "threshold_update"


def test_threshold_write_text_and_plat19_retimed():
    f = ThrFake(pace=300.0)
    cid = f.add("2026-11-19", run_cj())
    out = run(
        more.lo_sth_update_threshold_verified(
            "T1", "run_pace", "5:10", apply_text="write", today="2026-11-16", client_factory=lambda: f
        )
    )
    assert out["ok"] and out["landed"] == 310.0
    assert out["distance_retimed"][0]["targetSeconds"] == [3600, 3720]
    assert out["text"]["written"] == [{"id": cid, "date": "2026-11-19", "ok": True}]
    assert "6:33~6:10/km" in f.store[cid]["cj"]["summary"]
    assert any(w["code"] == "PLAT19_DISTANCE_RETIMED" for w in out["warnings"])


def test_threshold_not_landed_and_guards():
    f = ThrFake(ftp=200.0, land=False)
    f.add("2026-11-18", bike_cj())
    out = run(
        more.lo_sth_update_threshold_verified(
            "T1", "ftp", 210, apply_text="write", today="2026-11-16", client_factory=lambda: f
        )
    )
    assert out["isError"] and out["error_code"] == "WRITE_NOT_LANDED"
    assert not any(t == "update_assigned_workout_patch" for t, _ in f.calls)
    assert (
        run(more.lo_sth_update_threshold_verified("T1", "hr", 170, client_factory=lambda: f))["error_code"]
        == "BAD_KIND"
    )
    same = run(more.lo_sth_update_threshold_verified("T1", "ftp", 200, client_factory=lambda: f))
    assert same["status"] == "no_change"


def test_threshold_dry_run_does_not_write():
    f = ThrFake(ftp=200.0)
    f.add("2026-11-18", bike_cj())
    out = run(
        more.lo_sth_update_threshold_verified(
            "T1", "ftp", 210, dry_run=True, apply_text="write", today="2026-11-16", client_factory=lambda: f
        )
    )
    assert out["ok"] and f.thr["power"] == 200.0 and "preview" in out["text"]
    assert [a.get("dry_run") for t, a in f.calls if t == "apply_threshold_update"] == [True]


def test_more_tools_registered_without_athlete_param():
    from tp_mcp.server import TOOLS

    names = {t.name: t for t in TOOLS}
    for n in more.LO_STH_MORE_TOOLS:
        assert n in names and "athlete" not in names[n].input_schema["properties"]
