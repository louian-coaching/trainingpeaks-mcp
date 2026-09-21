"""Batch planned-workout creation — LOCAL PATCH (羅教練 2026/08/21).

一次送整週課表，把排課 SOP Step 4 從 12–15 輪模型往返壓成 1 輪，並在同一次
呼叫內完成建課後讀回，輸出**可直接餵給 validate_week.py 的 JSON**。

設計邊界（正本＝排課專案 `tp-ai-layer/tools/tp_create_workouts_batch_規格.md`）：
  - 只做寫入與讀回，**不做任何設計決策**
  - **不做方法論檢查**（R1–R27 由排課專案的 validate_week.py 負責，跨 repo 不依賴）
  - 不刪課、不改既有課（`skip_if_exists` 只跳過、不覆蓋）
  - **絕不自動重試**：逾時／不確定一律標 `uncertain`，交由呼叫端讀回確認
    （TP 可能已建成但回應遺失，重試＝製造重複課）

git pull 後若本檔消失或 server.py 的註冊段不見了，用排課專案
`tp-ai-layer/tools/tp-mcp-batch-create.patch` 重套（git apply）。
"""

import logging
from typing import Any

from pydantic import ValidationError

from tp_mcp.tools._validation import CreateWorkoutInput, format_validation_error
from tp_mcp.tools.profile import tp_get_profile
from tp_mcp.tools.workouts import (
    tp_create_workout,
    tp_get_workout,
    tp_get_workouts,
    tp_update_workout,
)

logger = logging.getLogger("tp-mcp")

MAX_BATCH = 30

_PASSTHROUGH_FIELDS = (
    "duration_minutes", "description", "distance_km", "tss_planned",
    "structure", "structured_workout", "subtype_id", "tags",
    "feeling", "rpe", "is_hidden",
)


def _err(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"isError": True, "error_code": code, "message": message, **extra}


def _day(value: Any) -> str:
    """Normalize a date/datetime string to YYYY-MM-DD for duplicate keying."""
    return str(value or "")[:10]


def _dup_key(date: Any, sport: Any, title: Any) -> tuple[str, str, str]:
    """R7 duplicate key: same day + same sport + same title."""
    return (_day(date), str(sport or "").strip(), str(title or "").strip())


def _flatten_readback(detail: dict[str, Any]) -> dict[str, Any]:
    """Reshape tp_get_workout output into the shape validate_week.py expects.

    ⚠ 這是本工具的實質價值之一。兩處落差：
      1. `tp_get_workouts`（列表）**不回傳 structured_workout**，而 R2/R3/R4/R12
         都要它 → 讀回必須逐堂走 `tp_get_workout`。
      2. `tp_get_workout` 把 duration_planned／tss_planned 放在 `metrics` 子物件，
         validate_week.py 讀的是**頂層** → 這裡攤平。
    """
    metrics = detail.get("metrics") or {}
    return {
        "id": detail.get("id"),
        "date": detail.get("date"),
        "title": detail.get("title"),
        "sport": detail.get("sport"),
        "type": "completed" if detail.get("completed") else "planned",
        "description": detail.get("description"),
        "duration_planned": metrics.get("duration_planned"),
        "duration_actual": metrics.get("duration_actual"),
        "tss_planned": metrics.get("tss_planned"),
        "tss_actual": metrics.get("tss_actual"),
        "distance_planned_km": metrics.get("distance_planned_km"),
        "structured_workout": detail.get("structured_workout"),
    }


def _echo(sent: dict[str, Any], flat: dict[str, Any]) -> dict[str, Any]:
    """Compact landed-value proof, so the caller needn't re-read the workout.

    取代 verify_update.py 的獨立一輪：description 逐字比對 + begin/end 首末值
    （距離型跑課要是累計公尺不是秒，TECH-05）+ polyline 點數。
    """
    sw = flat.get("structured_workout") or {}
    structure = sw.get("structure") or []
    polyline = sw.get("polyline") or []
    sent_desc = sent.get("description")
    got_desc = flat.get("description")

    first_begin = last_end = None
    if isinstance(structure, list) and structure:
        if isinstance(structure[0], dict):
            first_begin = structure[0].get("begin")
        if isinstance(structure[-1], dict):
            last_end = structure[-1].get("end")

    return {
        "tss_planned": flat.get("tss_planned"),
        "duration_planned": flat.get("duration_planned"),
        "primary_length_metric": sw.get("primaryLengthMetric"),
        "structure_begin": first_begin,
        "structure_end": last_end,
        "structure_blocks": len(structure) if isinstance(structure, list) else 0,
        "polyline_points": len(polyline) if isinstance(polyline, list) else 0,
        "description_len": len(got_desc) if isinstance(got_desc, str) else 0,
        "description_match": (sent_desc or "") == (got_desc or ""),
    }


def _row_warnings(item: dict[str, Any], flat: dict[str, Any] | None,
                  echo: dict[str, Any] | None) -> list[str]:
    warnings: list[str] = []
    sport = str(item.get("sport") or "")

    if sport == "Strength":
        warnings.append(
            "Strength 課走的是舊 description 寫法（中國區）。非中國區選手應改用 "
            "tp_create_strength_workout（TECH-18／R8-SB），此工具不驗證這一點。"
        )
    if echo is None:
        return warnings

    if not echo.get("description_match"):
        warnings.append("description 讀回與送出值不符——請人工比對，本工具不自動重送。")
    if sport in ("Bike", "Run"):
        if not echo.get("tss_planned"):
            warnings.append("tss_planned 未落地（R2 會 FAIL）。")
        if not echo.get("duration_planned"):
            warnings.append("duration_planned 未落地（R2 會 FAIL）。")
        if echo.get("structure_blocks", 0) == 0:
            warnings.append("structured_workout 未落地（R2 會 FAIL）。")
    if echo.get("primary_length_metric") == "distance":
        end = echo.get("structure_end")
        if isinstance(end, (int, float)) and 0 < end < 1000:
            warnings.append(
                f"距離型課的 structure end＝{end}，看起來像秒值不是累計公尺"
                "（TECH-05：TP 對距離型不會重算，彈窗大圖會糊掉）。"
            )
    return warnings


def _wants_zero_duration(item: dict[str, Any]) -> bool:
    """DayOff 的 duration 不能是 0 或空 → 先建 1 分鐘、建完再改 0（既有眉角）。"""
    return str(item.get("sport") or "") == "DayOff" and not item.get("duration_minutes")


def _effective_fields(item: dict[str, Any]) -> dict[str, Any]:
    """Passthrough fields with the DayOff placeholder applied.

    預驗與實際建課走同一份欄位，否則 DayOff 會在預驗被
    CreateWorkoutInput 的「duration 或 structure 至少要有一個」擋下來。
    """
    fields = {f: item.get(f) for f in _PASSTHROUGH_FIELDS}
    if _wants_zero_duration(item):
        fields["duration_minutes"] = 1
    return fields


async def _create_one(item: dict[str, Any]) -> dict[str, Any]:
    """Create one workout, handling the DayOff zero-duration quirk."""
    wants_zero = _wants_zero_duration(item)

    kwargs: dict[str, Any] = {
        "date_str": item["date"],
        "sport": str(item.get("sport") or ""),
        "title": item["title"],
    }
    kwargs.update({k: v for k, v in _effective_fields(item).items() if v is not None})

    created = await tp_create_workout(**kwargs)
    if created.get("isError"):
        return created

    if wants_zero and created.get("workout_id") is not None:
        zeroed = await tp_update_workout(
            workout_id=str(created["workout_id"]), duration_minutes=0,
        )
        created["dayoff_zeroed"] = not zeroed.get("isError")
    return created


async def tp_create_workouts_batch(
    workouts: list[dict[str, Any]],
    expect_athlete_name: str | None = None,
    dry_run: bool = False,
    on_error: str = "stop",
    skip_if_exists: bool = True,
    readback: bool = True,
    readback_save_to: str | None = None,
    readback_week_start: str | None = None,
    readback_week_end: str | None = None,
    target_tss: str | None = None,
) -> dict[str, Any]:
    """Create a whole week of planned workouts in one call.

    Args:
        workouts: List of workout payloads (same fields as tp_create_workout,
            with `date` instead of `date_str`).
        expect_athlete_name: If given and it does not match the resolved
            athlete, **the whole batch is rejected and nothing is created**.
        dry_run: Validate + identity check only; create nothing.
        on_error: "stop" (default) or "continue".
        skip_if_exists: Skip a workout when same day + sport + title already
            exists in the target range (R7 duplicate guard, makes reruns safe).
        readback: Re-read each created workout and return landed-value proof.
        readback_week_start: With readback_week_end, readback_save_to receives the
            WHOLE week instead of only the rows just created — week-level rules
            (R6/R14/R45) read the partial file as false FAILs otherwise.
        readback_week_end: See readback_week_start.
        target_tss: e.g. "470-515". With a whole-week readback, returns
            week_load {tri_tss, in_range, gap} so the week does not need a
            separate lo_get_week_for_validate call to be weighed.
        readback_save_to: Absolute path. When set, the readback block is written
            there (ready for `validate_week.py <path>`) and only the path is
            returned — keeps a full week of polylines out of the model context.

    Returns:
        Dict with athlete identity, per-row results, and a `readback` block
        shaped for validate_week.py.
    """
    if not isinstance(workouts, list) or not workouts:
        return _err("VALIDATION_ERROR", "workouts 必須是非空陣列。")
    if len(workouts) > MAX_BATCH:
        return _err(
            "VALIDATION_ERROR",
            f"一次最多 {MAX_BATCH} 堂，收到 {len(workouts)} 堂。",
        )
    if on_error not in ("stop", "continue"):
        return _err("VALIDATION_ERROR", "on_error 只能是 'stop' 或 'continue'。")

    # --- 1. 全批語法預驗：任一筆不合法就一堂都不建 -------------------------
    errors: list[dict[str, Any]] = []
    for index, item in enumerate(workouts):
        if not isinstance(item, dict):
            errors.append({"index": index, "message": "每一筆必須是物件。"})
            continue
        try:
            CreateWorkoutInput(
                date=item.get("date"),
                sport=item.get("sport"),
                title=item.get("title"),
                **_effective_fields(item),
            )
        except (ValidationError, ValueError) as exc:
            msg = (
                format_validation_error(exc)
                if isinstance(exc, ValidationError) else str(exc)
            )
            errors.append({
                "index": index,
                "date": _day(item.get("date")),
                "title": item.get("title"),
                "message": msg,
            })

    # 批內重複（同日同運動同標題）也在寫入前擋掉
    seen: dict[tuple[str, str, str], int] = {}
    for index, item in enumerate(workouts):
        if not isinstance(item, dict):
            continue
        key = _dup_key(item.get("date"), item.get("sport"), item.get("title"))
        if key in seen:
            errors.append({
                "index": index,
                "date": key[0],
                "title": key[2],
                "message": f"與第 {seen[key]} 筆重複（同日同運動同標題，R7）。",
            })
        else:
            seen[key] = index

    if errors:
        return _err(
            "PREFLIGHT_FAILED",
            f"{len(errors)} 筆未通過語法預驗，整批未寫入。",
            errors=errors,
            created=0,
        )

    # --- 2. 身分核對：批次寫錯人的代價是整週要刪，必須擋在寫入前 -----------
    profile = await tp_get_profile()
    if profile.get("isError"):
        return _err(
            "IDENTITY_CHECK_FAILED",
            "無法取得選手身分，整批未寫入。athlete 參數是否正確？",
            detail=profile.get("message"),
        )
    athlete_name = profile.get("name")
    athlete_id = profile.get("athlete_id")

    if expect_athlete_name:
        got = str(athlete_name or "").strip().lower()
        want = expect_athlete_name.strip().lower()
        if want not in got and got not in want:
            return _err(
                "ATHLETE_MISMATCH",
                f"身分不符：expect_athlete_name='{expect_athlete_name}'，"
                f"實際解析到 '{athlete_name}'。整批未寫入、一堂都沒建。",
                athlete_name=athlete_name,
                athlete_id=athlete_id,
            )

    dates = sorted(_day(w.get("date")) for w in workouts)
    date_range = {"start": dates[0], "end": dates[-1]}

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "athlete_id": athlete_id,
            "athlete_name": athlete_name,
            "date_range": date_range,
            "would_create": len(workouts),
            "note": (
                "語法預驗與身分核對通過，未寫入任何課。"
                "方法論檢查（R1–R27）請在送出前跑 validate_week.py。"
            ),
        }

    # --- 3. 冪等：先讀既有 planned 課，同鍵跳過（讓中斷後可安全重跑）-------
    existing: set[tuple[str, str, str]] = set()
    preexisting: list[dict[str, Any]] = []
    if skip_if_exists:
        found = await tp_get_workouts(
            start_date=date_range["start"],
            end_date=date_range["end"],
            workout_filter="planned",
        )
        if found.get("isError"):
            return _err(
                "PRECHECK_FAILED",
                "skip_if_exists 需要先讀既有課，但讀取失敗，整批未寫入。"
                "確認無重複風險後可帶 skip_if_exists=false 重試。",
                detail=found.get("message"),
            )
        for w in found.get("workouts", []):
            existing.add(_dup_key(w.get("date"), w.get("sport"), w.get("title")))

        # 同一批的日期上，已經有別的課了嗎？（2026/09/21 加）
        # skip_if_exists 只擋「同日＋同項目＋同標題」；教練或選手自己先建的
        # 行程課、賽事佔位、團練，標題不同就完全不會被提到，於是同一天長出
        # 兩堂內容重疊的課（2026/09/21 Mark Huang 10/1 兩堂「移動日」）。
        # 這裡不擋、只把它們列出來——判斷是重複還是本來就該共存，是教練的事。
        batch_keys = {
            _dup_key(w.get("date"), w.get("sport"), w.get("title")) for w in workouts
        }
        target_days = {_day(w.get("date")) for w in workouts}
        preexisting = [
            {
                "id": w.get("id"), "date": w.get("date"), "sport": w.get("sport"),
                "title": w.get("title"), "tss_planned": w.get("tss_planned"),
            }
            for w in found.get("workouts", [])
            if _day(w.get("date")) in target_days
            and _dup_key(w.get("date"), w.get("sport"), w.get("title")) not in batch_keys
        ]

    # --- 4. 循序建課（不併發：TP 端無交易語意，出錯時要能說清楚建到哪）----
    results: list[dict[str, Any]] = []
    stopped_at: int | None = None

    for index, item in enumerate(workouts):
        row: dict[str, Any] = {
            "index": index,
            "date": _day(item.get("date")),
            "title": item.get("title"),
            "sport": item.get("sport"),
        }
        key = _dup_key(item.get("date"), item.get("sport"), item.get("title"))

        if key in existing:
            row.update({"status": "skipped", "reason": "同日同運動同標題已存在（R7）"})
            results.append(row)
            continue

        try:
            created = await _create_one(item)
        except Exception as exc:  # noqa: BLE001 — 網路／逾時：狀態不明
            logger.exception("Batch create failed at index %s", index)
            row.update({
                "status": "uncertain",
                "error": f"{type(exc).__name__}: {exc}",
                "note": (
                    "呼叫未回應，TP 端可能已建成也可能沒有。"
                    "**不自動重試**——請讀回該日課表確認後再決定。"
                ),
            })
            results.append(row)
            stopped_at = index
            break

        if created.get("isError"):
            row.update({
                "status": "failed",
                "error_code": created.get("error_code"),
                "error": created.get("message"),
            })
            results.append(row)
            if on_error == "stop":
                stopped_at = index
                break
            continue

        row.update({"status": "created", "workout_id": created.get("workout_id")})
        if created.get("dayoff_zeroed") is not None:
            row["dayoff_zeroed"] = created["dayoff_zeroed"]
        existing.add(key)
        results.append(row)

    # --- 5. 讀回：逐堂 tp_get_workout（列表 API 沒有 structured_workout）---
    readback_rows: list[dict[str, Any]] = []
    # strict=False 是刻意的：on_error="stop" 時 results 比 workouts 短，
    # 只要配對到已處理的那幾筆即可（results 順序與 workouts 前 N 筆一致）。
    if readback:
        for row, item in zip(results, workouts, strict=False):
            if row.get("status") != "created" or not row.get("workout_id"):
                continue
            detail = await tp_get_workout(workout_id=str(row["workout_id"]))
            if detail.get("isError"):
                row["warnings"] = ["讀回失敗，落地狀態未驗證。"]
                continue
            flat = _flatten_readback(detail)
            readback_rows.append(flat)
            row["echo"] = _echo(item, flat)
            row["warnings"] = _row_warnings(item, flat, row["echo"])

    for row, item in zip(results, workouts, strict=False):
        if "warnings" not in row:
            row["warnings"] = _row_warnings(item, None, None)

    counts = {"created": 0, "skipped": 0, "failed": 0, "uncertain": 0}
    for row in results:
        status = row.get("status", "")
        if status in counts:
            counts[status] += 1

    out: dict[str, Any] = {
        "success": counts["failed"] == 0 and counts["uncertain"] == 0,
        "athlete_id": athlete_id,
        "athlete_name": athlete_name,
        "date_range": date_range,
        "summary": {"total": len(workouts), **counts,
                    "not_attempted": len(workouts) - len(results)},
        "results": results,
    }

    if preexisting:
        out["preexisting_on_target_days"] = preexisting
        out["preexisting_note"] = (
            f"目標日期上另有 {len(preexisting)} 堂既有課（標題與本批不同，未被 "
            "skip_if_exists 擋下）。確認是刻意共存還是重複——刪課屬「先問再做」。"
        )

    # 直接餵 validate_week.py：欄位已攤平成該腳本讀的形狀
    readback_block = {"workouts": readback_rows, "count": len(readback_rows)}
    if readback_save_to and readback_week_start and readback_week_end:
        # 整週讀回（2026/09/21 加，與 lo_update_workouts_batch 同一個理由）：
        # 只寫剛建的幾堂，R6／R14／R45 這類週層級規則看不到同週其他課，
        # 會報假 FAIL；週量也還要再跑一次 lo_get_week_for_validate 才算得出來。
        from tp_mcp.tools.lo_tools import lo_get_week_for_validate, week_load_line

        week = await lo_get_week_for_validate(
            start_date=readback_week_start,
            end_date=readback_week_end,
            save_to=readback_save_to,
        )
        if isinstance(week, dict) and week.get("isError"):
            out["warnings"] = [f"whole-week readback failed: {week.get('message')}"]
            out["readback"] = readback_block
        else:
            out["readback_json_path"] = readback_save_to
            out["readback_scope"] = "week"
            out["readback_count"] = week.get("count")
            out["week_load"] = week_load_line(week.get("rows") or [], target_tss)
    elif readback_save_to:
        try:
            import json
            import os
            from pathlib import Path

            path = Path(os.path.expanduser(readback_save_to))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(readback_block, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            out["readback_json_path"] = str(path)
            out["readback_count"] = len(readback_rows)
            out["readback_scope"] = "created_rows_only"
            out["readback_note"] = (
                "檔案裡只有這批剛建的課；R6／R14／R45 這類週層級規則要帶 "
                "readback_week_start/end，或另跑 lo_get_week_for_validate。"
            )
        except OSError as exc:
            out["readback"] = readback_block
            out["readback_save_error"] = f"寫檔失敗，改回傳內容：{exc}"
    else:
        out["readback"] = readback_block

    if stopped_at is not None:
        out["stopped_at_index"] = stopped_at
        out["note"] = (
            f"於第 {stopped_at} 筆停止（on_error='{on_error}'）。"
            f"已建立的 {counts['created']} 堂保留在 TP 上、未回溯刪除"
            "（刪課屬「先問再做」）。修正後可帶 skip_if_exists=true 重跑同一份 payload。"
        )
    return out
