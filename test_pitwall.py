#!/usr/bin/env python3
from pitwall import build_snapshots, build_state, build_state_at, sparkline_path


data = {
    "drivers": [
        {"driver_number": 1, "name_acronym": "AAA", "team_colour": "FF0000"},
        {"driver_number": 2, "name_acronym": "BBB", "team_colour": "00FF00"},
    ],
    "position": [
        {"driver_number": 1, "position": 1, "date": "2026-01-01T12:00:00Z"},
        {"driver_number": 2, "position": 2, "date": "2026-01-01T12:00:00Z"},
    ],
    "intervals": [
        {"driver_number": 1, "gap_to_leader": 0, "interval": None, "date": "2026-01-01T12:00:00Z"},
        {"driver_number": 2, "gap_to_leader": 10.0, "interval": 10.0, "date": "2026-01-01T12:00:00Z"},
    ],
    "laps": [
        {"driver_number": 1, "lap_number": 10, "lap_duration": 90.0},
        {"driver_number": 1, "lap_number": 11, "lap_duration": 91.0},
        {"driver_number": 2, "lap_number": 10, "lap_duration": 92.0},
    ],
    "stints": [
        {"driver_number": 1, "stint_number": 1, "lap_start": 1, "compound": "MEDIUM", "tyre_age_at_start": 0},
        {"driver_number": 2, "stint_number": 2, "lap_start": 8, "compound": "HARD", "tyre_age_at_start": 1},
    ],
    "race_control": [{"date": "2026-01-01T12:00:01Z", "lap_number": 10, "message": "DRS ENABLED"}],
    "weather": [{"date": "2026-01-01T12:00:00Z", "air_temperature": 24, "track_temperature": 35, "rainfall": 0}],
}

state = build_state(data, pit_loss=5)
assert [row["name"] for row in state["board"]] == ["AAA", "BBB"]
assert state["board"][0]["recent_pace_seconds"] == 90.5
assert state["board"][1]["tyre_age_laps"] == 3
assert state["board"][0]["estimated_rejoin_position"] == 2
assert state["race_control"][0]["message"] == "DRS ENABLED"
assert state["board"][0]["pace_sparkline_path"].startswith("M0")
assert state["board"][0]["stint_history"] == [{"compound": "MEDIUM", "laps": 11}]

# as-of filtering: records after t are invisible; earlier t sees strictly less
data2 = {
    "drivers": data["drivers"],
    "position": [
        {"driver_number": 1, "position": 2, "date": "2026-01-01T12:00:00+00:00"},
        {"driver_number": 1, "position": 1, "date": "2026-01-01T12:04:00+00:00"},
        {"driver_number": 2, "position": 1, "date": "2026-01-01T12:00:00+00:00"},
        {"driver_number": 2, "position": 2, "date": "2026-01-01T12:04:00+00:00"},
    ],
    "intervals": [
        {"driver_number": 2, "gap_to_leader": 3.0, "interval": 3.0, "date": "2026-01-01T12:04:00+00:00"},
    ],
    "laps": [
        {"driver_number": 1, "lap_number": 1, "lap_duration": 90.0, "date_start": "2026-01-01T12:00:00+00:00"},
        {"driver_number": 1, "lap_number": 2, "lap_duration": 89.0, "date_start": "2026-01-01T12:02:00+00:00"},
        {"driver_number": 1, "lap_number": 3, "lap_duration": 88.0, "date_start": "2026-01-01T12:05:00+00:00"},
    ],
    "stints": [], "race_control": [], "weather": [],
}
early = build_state_at(data2, "2026-01-01T12:01:00+00:00")
late = build_state_at(data2, "2026-01-01T12:05:00+00:00")
early_p1 = next(r for r in early["board"] if r["driver_number"] == 1)
late_p1 = next(r for r in late["board"] if r["driver_number"] == 1)
assert early_p1["position"] == 2 and late_p1["position"] == 1  # position flip only visible late
assert early_p1["recent_pace_seconds"] == 90.0                 # one lap known at t_early
assert late_p1["recent_pace_seconds"] == 89.0                  # median of three at t_late

# snapshots: span 12:00:00..12:05:00 at 60s = 6 states inclusive
assert len(build_snapshots(data2, every=60)) == 6

# sparkline geometry
assert sparkline_path([]) == ""
flat = sparkline_path([90, 90, 90])
assert flat.startswith("M0.0,10.0") and flat.count("L") == 2

print("F1 Pit Wall check passed")
