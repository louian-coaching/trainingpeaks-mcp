"""lo_verify._lap_km — distance inside a lap survives a glitch in the distance channel."""

from tp_mcp.tools.lo_verify import _lap_km


def _pts(dists, t0=0):
    return [{"time": t0 + i, "Distance": d} for i, d in enumerate(dists)]


def test_steady_lap():
    assert _lap_km(_pts([6.000 + 0.0035 * i for i in range(229)])) == 0.8


def test_drop_to_near_zero_after_a_pause_is_ignored():
    walk = [6.970 + 0.0013 * i for i in range(60)]
    glitch = [0.004]                                   # resume sample
    more = [walk[-1] + 0.0013 * (i + 1) for i in range(70)]
    assert _lap_km(_pts(walk + glitch + more)) == 0.17   # not 7.1


def test_no_usable_samples():
    assert _lap_km([{"time": 1}, {"Distance": 2.0}]) is None
