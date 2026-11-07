"""FORK 2026-11-06: lo_sweep_readbacks."""

import json

import pytest

from tp_mcp.client.context import athlete_override
from tp_mcp.tools import lo_weekly


@pytest.mark.asyncio
async def test_sweep_writes_one_file_per_athlete_and_manifest(tmp_path, monkeypatch):
    ids = {"Justin Lin": ("Justin Lin", 3742891), "Yema": ("Yema Selena", 4025942), "bad": (None, None)}
    seen_snapshot = []

    async def fake_ident():
        name, aid = ids[athlete_override.get()]
        return {"athlete_name": name, "athlete_id": aid} if aid else {}

    async def fake_week(start_date, end_date, save_to, _snapshot=True, **kw):
        seen_snapshot.append(_snapshot)
        with open(save_to, "w") as fh:
            fh.write(json.dumps({"workouts": []}))
        return {"success": True, "count": 0}

    import tp_mcp.tools.lo_review as lr
    import tp_mcp.tools.lo_tools as lt
    monkeypatch.setattr(lr, "athlete_identity", fake_ident)
    monkeypatch.setattr(lt, "lo_get_week_for_validate", fake_week)
    out = await lo_weekly.lo_sweep_readbacks(["Justin Lin", "Yema", "bad"], "2026-11-09", "2026-11-22",
                                             str(tmp_path))
    assert out["ok"] == 2 and len(out["errors"]) == 1
    assert (tmp_path / "3742891.json").exists() and (tmp_path / "4025942.json").exists()
    man = json.loads((tmp_path / "manifest.json").read_text())
    assert man["window"] == ["2026-11-09", "2026-11-22"] and len(man["athletes"]) == 3
    assert seen_snapshot == [False, False]
