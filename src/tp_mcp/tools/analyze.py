"""Tool for workout analysis via the Peaksware analysis API.

TrainingPeaks retired the monolithic v1 endpoint
(``POST /workout-analysis/v1/analyze``) — confirmed dead 2026-07 (every
workout, every account, every age, 404 with an empty body from the real
backend, not the gateway — auth was never the issue). Live browser-network
tracing showed the current web app instead calls three narrower v2
endpoints per workout:

  * ``POST /workout-analysis/v2/analyze/summary`` — whole-workout totals
    (TSS/IF/NP/EF/decoupling/...).
  * ``POST /workout-analysis/v2/analyze/charts``  — per-second time-series
    plus per-channel min/max/average/zones.
  * ``POST /workout-analysis/v2/analyze/laps``    — device-recorded laps
    (WorkoutStepIndex/Intensity/LapTrigger/AveragePace/...).

All three take a body of just ``{"workoutId": <id>}`` — no
``viewingPersonId`` needed even for a coach viewing an athlete's workout
(verified live: the workout id alone is sufficient, confirmed with and
without the field for a coach-owned athlete).

The three responses are merged back into the same shape the rest of this
codebase (and any downstream consumer) already expects from
``parse_workout_analysis`` — so only this fetch layer needed to change.
``summary`` is required (its absence means the workout genuinely isn't
analyzable, mirroring the old v1 404 semantics); ``charts``/``laps`` degrade
gracefully to empty on a 404 (some entries — e.g. manually logged, no device
file — legitimately lack per-second/lap data while still having totals).
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from tp_mcp.client import TPClient, parse_workout_analysis
from tp_mcp.tools._validation import WorkoutIdInput, format_validation_error

logger = logging.getLogger("tp-mcp")

ANALYSIS_API_BASE = "https://api.peakswaresb.com"
ANALYSIS_TIMEOUT = 60.0
ANALYSIS_DATA_DIR = Path(tempfile.gettempdir()) / "tp-mcp" / "analysis"

_SUMMARY_PATH = "/workout-analysis/v2/analyze/summary"
_CHARTS_PATH = "/workout-analysis/v2/analyze/charts"
_LAPS_PATH = "/workout-analysis/v2/analyze/laps"


def _save_analysis_json(
    workout_id: int, data: dict[str, Any], save_to: str | None = None
) -> str:
    """Save full analysis data (including time-series) to a JSON file.

    FORK 2026-09-21: ``save_to`` redirects the dump out of this process's
    private tempdir. The default path is only reachable from inside the server,
    so anything wanting to check the time series itself (a validation script, a
    coach's notebook) could never open it. The generic dispatch-level ``save_to``
    is no help here — it writes the tool's *return value*, which deliberately
    leaves the time series out.

    Returns:
        Absolute path to the saved file.
    """
    if save_to:
        filepath = Path(os.path.expanduser(save_to))
        filepath.parent.mkdir(parents=True, exist_ok=True)
    else:
        ANALYSIS_DATA_DIR.mkdir(parents=True, exist_ok=True)
        filepath = ANALYSIS_DATA_DIR / f"workout_{workout_id}.json"
    filepath.write_text(json.dumps(data, indent=2))
    return str(filepath)


def _error_for_status(status_code: int, workout_id: str) -> dict[str, Any] | None:
    """Map a non-200 analysis-API status to our error envelope, or None for 200."""
    if status_code == 401:
        return {
            "isError": True,
            "error_code": "AUTH_EXPIRED",
            "message": "Session expired. Run 'tp-mcp auth' to re-authenticate.",
        }
    if status_code == 404:
        return {
            "isError": True,
            "error_code": "NOT_FOUND",
            "message": f"Workout {workout_id} not found for analysis.",
        }
    if status_code != 200:
        return {
            "isError": True,
            "error_code": "API_ERROR",
            "message": f"Analysis API error: {status_code}",
        }
    return None


async def _post_analysis(
    http_client: httpx.AsyncClient,
    path: str,
    headers: dict[str, str],
    workout_id: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """POST ``{"workoutId": workout_id}`` to a v2 analysis endpoint.

    Returns:
        ``(body, None)`` on success, or ``(None, error_envelope)``.
    """
    try:
        response = await http_client.post(
            f"{ANALYSIS_API_BASE}{path}",
            headers=headers,
            json={"workoutId": workout_id},
        )
    except httpx.TimeoutException:
        return None, {
            "isError": True,
            "error_code": "NETWORK_ERROR",
            "message": "Analysis request timed out.",
        }
    except httpx.RequestError:
        logger.exception("Network error during workout analysis (%s)", path)
        return None, {
            "isError": True,
            "error_code": "NETWORK_ERROR",
            "message": "A network error occurred.",
        }

    err = _error_for_status(response.status_code, str(workout_id))
    if err:
        return None, err

    try:
        return response.json(), None
    except Exception:
        return None, {
            "isError": True,
            "error_code": "API_ERROR",
            "message": "Failed to parse analysis response.",
        }


def _stop_timestamp(start_iso: str | None, elapsed_seconds: Any) -> str | None:
    """v2's summary endpoint only gives ``startTimestamp`` — derive the stop
    from ``TotalElapsedTime`` (wall-clock elapsed, includes any pauses), to
    keep feeding the same start/stop pair the rest of the codebase expects
    (multisport-day ordering, gap detection, local-time display)."""
    if not start_iso or not isinstance(elapsed_seconds, (int, float)):
        return None
    try:
        start_dt = datetime.fromisoformat(start_iso)
    except ValueError:
        return None
    return (start_dt + timedelta(seconds=float(elapsed_seconds))).isoformat()


def _alias_developer_power(data_elements: list[dict[str, Any]],
                           time_series: list[dict[str, Any]]) -> str | None:
    """FORK (TECH-47): expose a developer-field power channel as ``Power``.

    Stryd and other Connect IQ sensors write running power as a developer
    field: the channel's identifier is a hash (e.g. ``6835fb106ff4c1e3``) and
    only its ``friendlyName`` says "Power". Everything downstream — the compact
    channel list, ``lo_verify_intervals`` — keys on the standard ``Power``
    identifier, so these workouts read as "no power recorded". On 2026/10/01
    that sent the coach a wrong "check your Stryd upload" for two athletes
    whose files were complete.

    When there is no standard ``Power`` channel and exactly one channel named
    "Power", copy its samples to ``Power`` on every point and relabel the
    element (keeping the original id in ``source_identifier``). Returns the
    aliased identifier, or None when nothing changed. Mutates in place.
    """
    if any(e.get("identifier") == "Power" for e in data_elements):
        return None
    cands = [e for e in data_elements
             if (e.get("name") or "").strip().lower() == "power" and e.get("identifier")]
    if len(cands) != 1:
        return None
    elem = cands[0]
    src = elem["identifier"]
    for p in time_series:
        if isinstance(p, dict) and src in p and "Power" not in p:
            p["Power"] = p[src]
    elem["source_identifier"] = src
    elem["identifier"] = "Power"
    return src


_LAP_KEEP = (
    ("Name", "name"), ("Intensity", "class"), ("TotalTimerTime", "sec"), ("TotalDistance", "km"),
    ("AveragePace", "pace_s"), ("NormalizedGradedPace", "ngp_s"), ("AveragePower", "avg_w"),
    ("NormalizedPower", "np_w"), ("AverageHeartRate", "avg_hr"), ("MaximumHeartRate", "max_hr"),
    ("AverageCadence", "cad"), ("TotalAscent", "gain_m"), ("TSS", "tss"), ("rTSS", "rtss"),
    ("Lap Power", "lap_w"),  # FORK (TECH-47): Stryd developer-field lap power
)
_TOTAL_KEEP = ("Duration", "Moving time", "Distance", "TSS", "rTSS", "IF", "rIF", "NP", "NGP",
               "Avg Power", "Average Power", "Avg Pace", "EF", "Pw:Hr", "Pa:Hr", "El. Gain", "VI")
_CHANNEL_KEEP = ("HeartRate", "Power", "Pace", "Speed", "Cadence")


def compute_splits(time_series: list[dict[str, Any]], split_km: float) -> list[dict[str, Any]]:
    """FORK (2026/10/05): fixed-distance splits from the chart time series.

    Races recorded as one or three laps (Gou Cai 杭州 2024: 3 laps for 42 km)
    left the race plan / PROC-41c review reconstructing per-5-km pace by hand.
    Boundaries are linearly interpolated on (time, Distance) so a split's time
    is not quantised to the chart's sample spacing; ``moving_sec`` drops
    samples slower than 3 km/h (start-corral wait, stops). HR/power are
    time-weighted means of the samples inside the split; ``gain_m``/``loss_m``
    sum altitude moves of ≥2 m (hysteresis; SmoothedAltitude preferred). Resolution note: the
    chart stream is ~1000 points, i.e. ~10 s apart on a 3-hour run."""
    pts = [p for p in time_series
           if isinstance(p.get("time"), (int, float)) and isinstance(p.get("Distance"), (int, float))]
    if len(pts) < 2 or not split_km or split_km <= 0:
        return []
    pts.sort(key=lambda p: p["time"])
    total = pts[-1]["Distance"]

    def t_at(km: float) -> float:
        for i in range(1, len(pts)):
            d0, d1 = pts[i - 1]["Distance"], pts[i]["Distance"]
            if d1 >= km:
                t0, t1 = pts[i - 1]["time"], pts[i]["time"]
                if d1 == d0:
                    return float(t1)
                return t0 + (t1 - t0) * (km - d0) / (d1 - d0)
        return float(pts[-1]["time"])

    def alt(p: dict[str, Any]) -> Any:
        v = p.get("SmoothedAltitude")
        return v if isinstance(v, (int, float)) else p.get("Altitude")

    bounds = []
    k = 0.0
    while k < total - 1e-6:
        bounds.append((k, min(k + split_km, total)))
        k += split_km
    out: list[dict[str, Any]] = []
    for lo, hi in bounds:
        if hi - lo < 0.05:
            continue
        t_lo = float(pts[0]["time"]) if lo == 0 else t_at(lo)
        t_hi = t_at(hi)
        inside = [p for p in pts if lo <= p["Distance"] <= hi]
        stopped = 0.0
        hr_w = hr_t = pw_w = pw_t = 0.0
        gain = loss = 0.0
        # altitude hysteresis: GPS/baro jitter on a flat road read as 30 m of "climb";
        # the anchor starts at the last sample before the split so a step at the boundary counts once
        before = [p for p in pts if p["Distance"] < lo]
        anchor = alt(before[-1]) if before else None
        if not isinstance(anchor, (int, float)):
            anchor = None
        prev = None
        for p in inside:
            if prev is not None:
                dt = p["time"] - prev["time"]
                dd = p["Distance"] - prev["Distance"]
                if dt > 0 and dd / (dt / 3600.0) < 3.0:
                    stopped += dt
                for key, acc in (("HeartRate", "hr"), ("Power", "pw")):
                    v = p.get(key)
                    if isinstance(v, (int, float)) and v > 0 and dt > 0:
                        if acc == "hr":
                            hr_w += v * dt; hr_t += dt
                        else:
                            pw_w += v * dt; pw_t += dt
            a1 = alt(p)
            if isinstance(a1, (int, float)):
                if anchor is None:
                    anchor = a1
                elif a1 - anchor >= 2:
                    gain += a1 - anchor
                    anchor = a1
                elif anchor - a1 >= 2:
                    loss += anchor - a1
                    anchor = a1
            prev = p
        km = hi - lo
        sec = t_hi - t_lo
        row: dict[str, Any] = {"km_from": round(lo, 2), "km_to": round(hi, 2),
                               "sec": round(sec), "pace_s": round(sec / km)}
        moving = sec - stopped
        if stopped > sec * 0.03:
            row["moving_sec"] = round(moving)
            row["moving_pace_s"] = round(moving / km)
        if hr_t:
            row["avg_hr"] = round(hr_w / hr_t)
        if pw_t:
            row["avg_w"] = round(pw_w / pw_t)
        row["gain_m"] = round(gain)
        row["loss_m"] = round(loss)
        out.append(row)
    return out


def _compact(full: dict[str, Any]) -> dict[str, Any]:
    """FORK: lap table + key totals only (the full dump is still written to data_file)."""
    laps = []
    for lap in full.get("lapData") or []:
        row = {}
        for src, dst in _LAP_KEEP:
            v = lap.get(src)
            if v is not None and v != "":
                row[dst] = round(v, 2) if isinstance(v, float) else v
        laps.append(row)
    totals = {k: v.get("value") for k, v in (full.get("totals") or {}).items() if k in _TOTAL_KEEP}
    chans = {c["identifier"]: [c.get("min"), c.get("max"), c.get("average")]
             for c in full.get("dataChannels") or [] if c.get("identifier") in _CHANNEL_KEEP}
    return {
        "workoutId": full.get("workoutId"),
        "startTimestamp": full.get("startTimestamp"),
        "totals": totals,
        "channels_min_max_avg": chans,
        "laps": laps,
        "lap_units": "sec=timer seconds, pace_s/ngp_s=seconds per km, km=distance",
        "single_lap": full.get("single_lap"),
        "time_series_points": full.get("time_series_points"),
        **({"splits": full["splits"], "split_units": full.get("split_units")} if full.get("splits") else {}),
        "data_file": full.get("data_file"),
        "detail": "compact (pass detail='full' for zones, all lap columns and channel metadata)",
    }


async def tp_analyze_workout(workout_id: str, save_to: str | None = None, detail: str = "compact",
                             split_km: float | None = None) -> dict[str, Any]:
    """Get detailed workout analysis including metrics, zones, and lap data.

    Full time-series data is saved to a JSON file for further analysis.

    Args:
        workout_id: The workout ID (from tp_get_workouts).
        save_to: Absolute path for the full dump (time series included). Without
            it the dump lands in this process's tempdir, which nothing outside
            the server can open.

    Returns:
        Dict with totals, data channels, lap data, and path to full data file.
    """
    try:
        validated = WorkoutIdInput(workout_id=workout_id)
    except (ValidationError, ValueError) as e:
        msg = format_validation_error(e) if isinstance(e, ValidationError) else str(e)
        return {
            "isError": True,
            "error_code": "VALIDATION_ERROR",
            "message": msg,
        }
    wid = validated.workout_id

    async with TPClient() as client:
        athlete_id = await client.ensure_athlete_id()
        if not athlete_id:
            return {
                "isError": True,
                "error_code": "AUTH_INVALID",
                "message": "Could not get athlete ID. Re-authenticate.",
            }

        # Ensure we have a valid token (athlete_id may have come from cache
        # without triggering token exchange)
        token_result = await client._ensure_access_token()
        if not token_result.success:
            return {
                "isError": True,
                "error_code": "AUTH_INVALID",
                "message": token_result.message or "Failed to obtain access token.",
            }

        access_token = client._token_cache.access_token
        if not access_token:
            return {
                "isError": True,
                "error_code": "AUTH_INVALID",
                "message": "No access token available. Re-authenticate.",
            }

        # Analysis API is on a different domain than the main TP API,
        # so we make direct httpx calls with the Bearer token.
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/json",
            "Origin": "https://app.trainingpeaks.com",
            "Referer": "https://app.trainingpeaks.com/",
        }

        async with httpx.AsyncClient(timeout=ANALYSIS_TIMEOUT) as http_client:
            summary, err = await _post_analysis(http_client, _SUMMARY_PATH, headers, wid)
            if err:
                return err

            charts, charts_err = await _post_analysis(http_client, _CHARTS_PATH, headers, wid)
            if charts_err:
                if charts_err.get("error_code") == "NOT_FOUND":
                    logger.info("workout %s: no chart/stream data available", wid)
                    charts = None
                else:
                    return charts_err

            laps, laps_err = await _post_analysis(http_client, _LAPS_PATH, headers, wid)
            if laps_err:
                if laps_err.get("error_code") == "NOT_FOUND":
                    logger.info("workout %s: no lap data available", wid)
                    laps = None
                else:
                    return laps_err

    summary_data = (summary or {}).get("data") or {}
    # Key totals by ``friendlyName`` (e.g. "NP", "Distance") rather than the v2
    # identifier (e.g. "NormalizedPower", "TotalDistance") to preserve the same
    # total names the old v1 endpoint returned; fall back to the identifier when
    # a channel has no friendlyName.
    totals = [
        {"name": meta.get("friendlyName") or name, "value": meta.get("value"), "unit": meta.get("unit")}
        for name, meta in summary_data.items()
        if isinstance(meta, dict)
    ]
    start_ts = (summary or {}).get("startTimestamp")
    elapsed = (summary_data.get("TotalElapsedTime") or {}).get("value")
    stop_ts = _stop_timestamp(start_ts, elapsed)

    charts_metadata = (charts or {}).get("metadata") or {}
    data_elements = [
        {
            "identifier": ident,
            "name": meta.get("friendlyName"),
            "unit": meta.get("unit"),
            "min": meta.get("minimum"),
            "max": meta.get("maximum"),
            "average": meta.get("average"),
            "zones": meta.get("zones"),
        }
        for ident, meta in charts_metadata.items()
        if isinstance(meta, dict)
    ]
    time_series = (charts or {}).get("data") or []
    _alias_developer_power(data_elements, time_series)

    lap_column_meta = (laps or {}).get("columnMetadata") or {}
    lap_columns = [
        {"identifier": ident, **meta}
        for ident, meta in lap_column_meta.items()
        if isinstance(meta, dict)
    ]
    lap_data = (laps or {}).get("data") or []

    raw_data: dict[str, Any] = {
        "workoutId": wid,
        "startTimestamp": start_ts,
        "stopTimestamp": stop_ts,
        "totals": totals,
        "dataElements": data_elements,
        "data": time_series,
        "lapData": lap_data,
        "lapColumns": lap_columns,
    }

    try:
        analysis = parse_workout_analysis(raw_data)
    except Exception:
        logger.exception("Failed to parse workout analysis")
        return {
            "isError": True,
            "error_code": "API_ERROR",
            "message": "Failed to parse workout analysis.",
        }

    # Save full raw data (including time-series) to file
    data_file = _save_analysis_json(wid, raw_data, save_to)

    # Return summary inline, point to file for full data
    totals_out = {t.name: {"value": t.value, "unit": t.unit} for t in analysis.totals}

    channels = [
        {
            k: v
            for k, v in {
                "identifier": ch.identifier,
                "name": ch.name,
                "unit": ch.unit,
                "min": ch.min,
                "max": ch.max,
                "average": ch.average,
                "zones": ch.zones,
            }.items()
            if v is not None
        }
        for ch in analysis.data_elements
    ]

    full = {
        "workoutId": analysis.workout_id,
        "startTimestamp": analysis.start_timestamp,
        "stopTimestamp": analysis.stop_timestamp,
        "totals": totals_out,
        "dataChannels": channels,
        "lapData": analysis.lap_data,
        "lapColumns": analysis.lap_columns,
        # One lap means per-segment review cannot come from the device; it has
        # to be reconstructed from the prescription (lo_verify_intervals).
        "single_lap": len(analysis.lap_data) <= 1,
        "time_series_points": len(analysis.data),
        "data_file": data_file,
    }
    if split_km:
        try:
            full["splits"] = compute_splits(time_series, float(split_km))
            full["split_units"] = ("sec=elapsed (interpolated), pace_s=sec/km; moving_* only when stops "
                                   "(<3 km/h) took >3%; chart stream ~1000 points")
        except (TypeError, ValueError):
            logger.exception("split computation failed")
    return full if str(detail or "compact").lower() == "full" else _compact(full)
