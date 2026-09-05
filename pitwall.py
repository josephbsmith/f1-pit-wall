#!/usr/bin/env python3
"""Local OpenF1 session workstation."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


BASE = "https://api.openf1.org/v1"
ENDPOINTS = (
    "drivers", "position", "intervals", "laps", "stints", "race_control", "weather",
    "pit", "overtakes", "session_result", "starting_grid", "team_radio",
    "championship_drivers", "championship_teams",
)
_TOKEN = ""
_TOKEN_EXPIRES = 0.0
_TOKEN_LOCK = threading.Lock()
_REQUEST_LOCK = threading.Lock()
_NEXT_REQUEST = 0.0
_STATE_CACHE: dict[tuple[str, float], tuple[float, dict[str, Any]]] = {}
_STATE_LOCK = threading.Lock()


def access_token() -> str | None:
    """Return a current OpenF1 token, refreshing account credentials when needed."""
    global _TOKEN, _TOKEN_EXPIRES
    if manual := os.environ.get("OPENF1_TOKEN"):
        return manual
    with _TOKEN_LOCK:
        if _TOKEN and time.time() < _TOKEN_EXPIRES:
            return _TOKEN
        username = os.environ.get("OPENF1_USERNAME")
        password = os.environ.get("OPENF1_PASSWORD")
        if not username or not password:
            return None
        body = urllib.parse.urlencode({"username": username, "password": password}).encode()
        request = urllib.request.Request(
            "https://api.openf1.org/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "F1-Pit-Wall/1.0"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
        _TOKEN = payload["access_token"]
        _TOKEN_EXPIRES = time.time() + max(60, int(payload.get("expires_in", 3600)) - 60)
        return _TOKEN


def clear_cached_token() -> None:
    global _TOKEN, _TOKEN_EXPIRES
    with _TOKEN_LOCK:
        _TOKEN, _TOKEN_EXPIRES = "", 0.0


def fetch(endpoint: str, session: str | None = None, **params: Any) -> list[dict[str, Any]]:
    """Fetch one OpenF1 collection while respecting the public request ceiling."""
    global _NEXT_REQUEST
    if session is not None:
        params.setdefault("session_key", session)
    query = urllib.parse.urlencode(params)
    for attempt in range(4):
        request = urllib.request.Request(f"{BASE}/{endpoint}?{query}", headers={"User-Agent": "F1-Pit-Wall/1.0"})
        if token := access_token():
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with _REQUEST_LOCK:
                time.sleep(max(0.0, _NEXT_REQUEST - time.monotonic()))
                _NEXT_REQUEST = time.monotonic() + 0.36
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and os.environ.get("OPENF1_USERNAME") and attempt < 3:
                clear_cached_token()
                continue
            if exc.code != 429 or attempt == 3:
                raise
            time.sleep(2 ** attempt * 5)  # 5s, 10s, 20s — OpenF1 free tier rate limit
    raise RuntimeError("unreachable")


def fetch_session(session: str) -> dict[str, list[dict[str, Any]]]:
    """Fetch a session plus its weekend context and a bounded telemetry window."""
    sessions = fetch("sessions", session)
    info = sessions[-1] if sessions else {"session_key": session}
    meeting_key = info.get("meeting_key")
    data = {
        "session": sessions,
        "sessions": fetch("sessions", meeting_key=meeting_key) if meeting_key else sessions,
        "meeting": fetch("meetings", meeting_key=meeting_key) if meeting_key else [],
        **{endpoint: [] for endpoint in ENDPOINTS},
    }
    def fetch_optional(endpoint: str) -> list[dict[str, Any]]:
        try:
            return fetch(endpoint, session)
        except urllib.error.HTTPError as exc:
            if exc.code not in {404, 422}:
                raise
            return []

    race_only = {"intervals", "overtakes", "championship_drivers", "championship_teams"}
    endpoints = [endpoint for endpoint in ENDPOINTS if endpoint not in race_only or info.get("session_type") in {"Race", "Sprint"}]
    with ThreadPoolExecutor(max_workers=6) as pool:
        data.update(zip(endpoints, pool.map(fetch_optional, endpoints)))

    dates = [d for ep in ("position", "laps") for row in data[ep] if (d := _record_date(row))]
    if dates:
        anchor = max(dates)
        location_since = (anchor - timedelta(seconds=150)).isoformat()
        cars_since = (anchor - timedelta(seconds=5)).isoformat()
        telemetry_since = (anchor - timedelta(seconds=12)).isoformat()
        lap_counts: dict[int, int] = {}
        for lap in data["laps"]:
            if lap.get("driver_number") is not None:
                lap_counts[lap["driver_number"]] = lap_counts.get(lap["driver_number"], 0) + 1
        reference = max(lap_counts, key=lap_counts.get, default=None)
        streams = [
            ("location", {"driver_number": reference, "date>": location_since}),
            ("location", {"date>": cars_since}),
            ("car_data", {"date>": telemetry_since}),
        ]
        def fetch_stream(item: tuple[str, dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
            endpoint, params = item
            try:
                return endpoint, fetch(endpoint, session, **params)
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                return endpoint, []
        data["location"], data["car_data"] = [], []
        with ThreadPoolExecutor(max_workers=3) as pool:
            for endpoint, rows in pool.map(fetch_stream, streams):
                data[endpoint].extend(rows)
    else:
        data["location"], data["car_data"] = [], []
    return data


def latest(records: list[dict[str, Any]], key: str, date_field: str = "date") -> dict[Any, dict[str, Any]]:
    result = {}
    for record in records:
        identifier = record.get(key)
        if identifier is None:
            continue
        if identifier not in result or str(record.get(date_field, "")) >= str(result[identifier].get(date_field, "")):
            result[identifier] = record
    return result


def numeric_gap(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _record_date(record: dict[str, Any]) -> datetime | None:
    raw = record.get("date") or record.get("date_start")
    return datetime.fromisoformat(raw) if raw else None


def _seconds(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _best(values: list[Any]) -> float | None:
    numbers = [float(value) for value in values if isinstance(value, (int, float))]
    return min(numbers) if numbers else None


def build_state_at(data: dict[str, list[dict[str, Any]]], t: str | datetime, pit_loss: float = 22.0) -> dict[str, Any]:
    """State as known at time t: drop records dated after t.

    OpenF1 stints have no timestamp, so they are gated by each driver's latest
    known lap instead.
    """
    cutoff = t if isinstance(t, datetime) else datetime.fromisoformat(t)
    timed = {"position", "intervals", "laps", "race_control", "weather", "pit", "overtakes", "team_radio", "location", "car_data"}
    filtered = {
        endpoint: [r for r in records if (d := _record_date(r)) is None or d <= cutoff] if endpoint in timed else records
        for endpoint, records in data.items()
    }
    known_laps: dict[Any, int] = {}
    for lap in filtered.get("laps", []):
        number = lap.get("driver_number")
        known_laps[number] = max(known_laps.get(number, 0), int(lap.get("lap_number") or 0))
    filtered["stints"] = [
        stint for stint in data.get("stints", [])
        if int(stint.get("lap_start") or 1) <= known_laps.get(stint.get("driver_number"), 0)
    ]
    info = (data.get("session") or [{}])[-1]
    if (end := _record_date({"date": info.get("date_end")})) and cutoff < end:
        filtered["session_result"] = []
        filtered["championship_drivers"] = []
        filtered["championship_teams"] = []
    return build_state(filtered, pit_loss)


def session_bounds(data: dict[str, list[dict[str, Any]]]) -> tuple[datetime, datetime]:
    dates = [d for ep in ("position", "intervals", "laps") for r in data.get(ep, []) if (d := _record_date(r))]
    return min(dates), max(dates)


def build_snapshots(data: dict[str, list[dict[str, Any]]], every: int, pit_loss: float = 22.0) -> list[dict[str, Any]]:
    start, end = session_bounds(data)
    states, t = [], start
    while t <= end:
        states.append(build_state_at(data, t, pit_loss))
        t += timedelta(seconds=every)
    return states


def sparkline_path(values: list[float], width: int = 96, height: int = 28) -> str:
    """SVG path for a small line chart; empty string for no data."""
    if not values:
        return ""
    lo, hi = min(values), max(values)
    span = hi - lo
    step = width / max(len(values) - 1, 1)
    y = lambda v: height / 2 if span == 0 else height - (v - lo) / span * height
    pts = [(round(i * step, 1), round(y(v), 1)) for i, v in enumerate(values)]
    head = f"M{pts[0][0]},{pts[0][1]}"
    tail = " ".join(f"L{x},{y}" for x, y in pts[1:])
    return f"{head} {tail}" if tail else head


def build_state(data: dict[str, list[dict[str, Any]]], pit_loss: float = 22.0) -> dict[str, Any]:
    session = (data.get("session") or [{}])[-1]
    meeting = (data.get("meeting") or [{}])[-1]
    session_type = str(session.get("session_type") or "Race")
    is_race = session_type in {"Race", "Sprint"}
    drivers = {row["driver_number"]: row for row in data.get("drivers", [])}
    positions = latest(data.get("position", []), "driver_number")
    intervals = latest(data.get("intervals", []), "driver_number")
    current_stints = latest(data.get("stints", []), "driver_number", "stint_number")
    results = {row["driver_number"]: row for row in data.get("session_result", [])}
    grid = {row["driver_number"]: row for row in data.get("starting_grid", [])}
    telemetry = latest(data.get("car_data", []), "driver_number")
    locations = latest(data.get("location", []), "driver_number")
    championship = {row["driver_number"]: row for row in data.get("championship_drivers", [])}
    laps_by_driver: dict[int, list[dict[str, Any]]] = {}
    for lap in data.get("laps", []):
        if lap.get("driver_number") is not None:
            laps_by_driver.setdefault(lap["driver_number"], []).append(lap)
    stints_by_driver: dict[int, list[dict[str, Any]]] = {}
    for stint in data.get("stints", []):
        if stint.get("driver_number") is not None:
            stints_by_driver.setdefault(stint["driver_number"], []).append(stint)
    gaps = {
        number: numeric_gap(intervals.get(number, {}).get("gap_to_leader"))
        for number in set(drivers) | set(positions)
    }
    leader = min((item for item in positions.values() if item.get("position")), key=lambda row: row["position"], default=None)
    if leader:
        gaps[leader["driver_number"]] = 0.0
    pits_by_driver: dict[int, list[dict[str, Any]]] = {}
    for pit in data.get("pit", []):
        if pit.get("driver_number") is not None:
            pits_by_driver.setdefault(pit["driver_number"], []).append(pit)
    numbers = set(drivers) | set(positions) | set(laps_by_driver) | set(results)
    best_by_driver: dict[int, float | None] = {}
    for number in numbers:
        best_by_driver[number] = _best([
            lap.get("lap_duration") for lap in laps_by_driver.get(number, [])
            if not lap.get("is_pit_out_lap")
        ])
        if best_by_driver[number] is None and not is_race:
            duration = results.get(number, {}).get("duration")
            best_by_driver[number] = _best(duration) if isinstance(duration, list) else _seconds(duration)
    field_best = _best(list(best_by_driver.values()))
    ranked = sorted(numbers, key=lambda n: (best_by_driver[n] if best_by_driver[n] is not None else float("inf"), n))
    practice_rank = {number: index + 1 for index, number in enumerate(ranked)}

    board = []
    for number in numbers:
        position = positions.get(number, {})
        driver_laps = sorted(laps_by_driver.get(number, []), key=lambda row: row.get("lap_number", 0))
        valid_laps = [row for row in driver_laps if isinstance(row.get("lap_duration"), (int, float)) and not row.get("is_pit_out_lap")]
        valid = [float(row["lap_duration"]) for row in valid_laps]
        recent_pace = statistics.median(valid[-3:]) if valid else None
        best_lap = min(valid_laps, key=lambda row: row["lap_duration"], default={})
        last_lap = valid_laps[-1] if valid_laps else {}
        current_lap = max((row.get("lap_number", 0) for row in driver_laps), default=0)
        stint = current_stints.get(number, {})
        tyre_age = None
        if stint and current_lap:
            tyre_age = max(0, current_lap - int(stint.get("lap_start") or current_lap) + int(stint.get("tyre_age_at_start") or 0))
        gap = gaps.get(number)
        projected = None
        if gap is not None:
            projected_gap = gap + pit_loss
            projected = 1 + sum(1 for other in gaps.values() if other is not None and other < projected_gap)
            projected = min(projected, len(positions))
        profile = drivers.get(number, {})
        result = results.get(number, {})
        result_position = result.get("position")
        grid_position = grid.get(number, {}).get("position")
        classification = (
            position.get("position") or result_position or 999
            if is_race else result_position or practice_rank.get(number, 999)
        )
        pit_stops = sorted(pits_by_driver.get(number, []), key=lambda row: row.get("lap_number") or 0)
        car = telemetry.get(number, {})
        location = locations.get(number, {})
        q = result.get("duration") if isinstance(result.get("duration"), list) else []
        board.append(
            {
                "position": classification,
                "track_position": position.get("position"),
                "driver_number": number,
                "name": profile.get("name_acronym") or profile.get("full_name") or str(number),
                "full_name": profile.get("full_name") or profile.get("broadcast_name") or str(number),
                "team": profile.get("team_name"),
                "team_colour": profile.get("team_colour"),
                "gap_to_leader": intervals.get(number, {}).get("gap_to_leader", 0 if leader and number == leader["driver_number"] else None),
                "interval": intervals.get(number, {}).get("interval"),
                "compound": stint.get("compound"),
                "tyre_age_laps": tyre_age,
                "recent_pace_seconds": recent_pace,
                "best_lap_seconds": best_by_driver.get(number),
                "last_lap_seconds": _seconds(last_lap.get("lap_duration")),
                "gap_to_best_seconds": best_by_driver[number] - field_best if best_by_driver[number] is not None and field_best is not None else None,
                "best_sectors": [best_lap.get(f"duration_sector_{i}") for i in range(1, 4)],
                "last_sectors": [last_lap.get(f"duration_sector_{i}") for i in range(1, 4)],
                "mini_sectors": sum((last_lap.get(f"segments_sector_{i}") or [] for i in range(1, 4)), []),
                "speed_trap": max((value for lap in driver_laps for value in (lap.get("i1_speed"), lap.get("i2_speed"), lap.get("st_speed")) if isinstance(value, (int, float))), default=None),
                "laps_completed": len(driver_laps),
                "q1": q[0] if len(q) > 0 else None,
                "q2": q[1] if len(q) > 1 else None,
                "q3": q[2] if len(q) > 2 else None,
                "grid_position": grid_position,
                "position_change": (grid_position - classification) if isinstance(grid_position, int) and isinstance(classification, int) else None,
                "pit_stops": len(pit_stops),
                "last_stop_seconds": _seconds(pit_stops[-1].get("stop_duration")) if pit_stops else None,
                "lane_time_seconds": _seconds(pit_stops[-1].get("lane_duration") or pit_stops[-1].get("pit_duration")) if pit_stops else None,
                "status": "DSQ" if result.get("dsq") else "DNS" if result.get("dns") else "DNF" if result.get("dnf") else None,
                "telemetry": {key: car.get(key) for key in ("speed", "rpm", "n_gear", "throttle", "brake", "drs")},
                "location": {key: location.get(key) for key in ("x", "y", "z")},
                "championship": championship.get(number, {}),
                "estimated_rejoin_position": projected,
                "pace_sparkline_path": sparkline_path(valid[-8:]),
                "stint_history": [
                    {
                        "compound": s.get("compound"),
                        "laps": max(1, int(s.get("lap_end") or current_lap or s.get("lap_start") or 1)
                                    - int(s.get("lap_start") or 1) + 1),
                    }
                    for s in sorted(stints_by_driver.get(number, []), key=lambda s: s.get("stint_number") or 0)
                ],
            }
        )
    board.sort(key=lambda row: row["position"] if row["position"] is not None else 999)
    weather = max(data.get("weather", []), key=lambda row: str(row.get("date", "")), default={})
    messages = sorted(data.get("race_control", []), key=lambda row: str(row.get("date", "")))[-40:]
    names = {number: profile.get("name_acronym") or str(number) for number, profile in drivers.items()}
    events = [
        {"date": row.get("date"), "kind": "CONTROL", "label": row.get("flag") or row.get("category"), "message": row.get("message"), "lap": row.get("lap_number"), "phase": row.get("qualifying_phase")}
        for row in data.get("race_control", [])
    ] + [
        {"date": row.get("date"), "kind": "PIT", "label": names.get(row.get("driver_number")), "message": f"Pit lane · {row.get('lane_duration') or row.get('pit_duration') or '—'}s / stop {row.get('stop_duration') or '—'}s", "lap": row.get("lap_number")}
        for row in data.get("pit", [])
    ] + [
        {"date": row.get("date"), "kind": "PASS", "label": names.get(row.get("overtaking_driver_number")), "message": f"Passed {names.get(row.get('overtaken_driver_number'), row.get('overtaken_driver_number'))} for P{row.get('position')}", "lap": None}
        for row in data.get("overtakes", [])
    ] + [
        {"date": row.get("date"), "kind": "RADIO", "label": names.get(row.get("driver_number")), "message": "Team radio", "url": row.get("recording_url"), "lap": None}
        for row in data.get("team_radio", [])
    ]
    events = sorted(events, key=lambda row: str(row.get("date") or ""))

    location_groups: dict[int, list[dict[str, Any]]] = {}
    for row in data.get("location", []):
        if (row.get("driver_number") is not None and isinstance(row.get("x"), (int, float))
                and isinstance(row.get("y"), (int, float)) and (row["x"] or row["y"])):
            location_groups.setdefault(row["driver_number"], []).append(row)
    outline = max(location_groups.values(), key=len, default=[])
    step = max(1, len(outline) // 120)
    outline = outline[::step][-120:]
    all_points = [row for rows in location_groups.values() for row in rows]
    xs, ys = [row["x"] for row in all_points], [row["y"] for row in all_points]
    min_x, max_x = (min(xs), max(xs)) if xs else (0, 1)
    min_y, max_y = (min(ys), max(ys)) if ys else (0, 1)
    def point(row: dict[str, Any]) -> dict[str, float]:
        return {
            "x": round(5 + 90 * (row["x"] - min_x) / max(1, max_x - min_x), 2),
            "y": round(95 - 90 * (row["y"] - min_y) / max(1, max_y - min_y), 2),
        }
    cars = []
    for number, rows in location_groups.items():
        profile = drivers.get(number, {})
        cars.append({
            "driver_number": number,
            "name": names.get(number, str(number)),
            "team_colour": profile.get("team_colour"),
            **point(rows[-1]),
        })
    phase = next((row.get("qualifying_phase") for row in reversed(messages) if row.get("qualifying_phase")), None)
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "session": session,
        "meeting": meeting,
        "sessions": sorted(data.get("sessions", []), key=lambda row: str(row.get("date_start", ""))),
        "session_type": session_type,
        "phase": f"Q{phase}" if phase else None,
        "lap_number": max((row.get("lap_number", 0) for row in data.get("laps", [])), default=0),
        "pit_loss_seconds": pit_loss,
        "board": board,
        "weather": weather,
        "race_control": messages,
        "events": events,
        "track": {"outline": [point(row) for row in outline], "cars": cars},
        "championship_drivers": sorted(data.get("championship_drivers", []), key=lambda row: row.get("position_current") or 999),
        "championship_teams": sorted(data.get("championship_teams", []), key=lambda row: row.get("position_current") or 999),
    }


HTML_PATH = os.path.join(os.path.dirname(__file__), "index.html")


def state_for(session: str, pit_loss: float) -> dict[str, Any]:
    """Return a cached state; live aliases expire in time for the next poll."""
    key = (session, pit_loss)
    now = time.monotonic()
    with _STATE_LOCK:
        if cached := _STATE_CACHE.get(key):
            if cached[0] > now:
                return cached[1]
        state = build_state(fetch_session(session), pit_loss)
        info = state.get("session", {})
        end = _record_date({"date": info.get("date_end")})
        live = session == "latest" or end is None or datetime.now(timezone.utc) <= end + timedelta(minutes=5)
        _STATE_CACHE[key] = (now + (8 if live else 86400), state)
        return state


def serve(session: str, pit_loss: float, port: int, replay: str | None = None) -> None:
    replay_bytes = None
    if replay:
        with open(replay, "rb") as handle:
            replay_bytes = handle.read()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                url = urllib.parse.urlparse(self.path)
                query = urllib.parse.parse_qs(url.query)
                if url.path == "/snapshots.json" and replay_bytes is not None:
                    content = replay_bytes
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                elif url.path == "/state.json" and replay_bytes is None:
                    requested_session = query.get("session_key", [session])[0]
                    if requested_session != "latest" and not requested_session.isdigit():
                        raise ValueError("session_key must be numeric or latest")
                    requested_loss = min(45.0, max(10.0, float(query.get("pit_loss", [pit_loss])[0])))
                    response = {**state_for(requested_session, requested_loss), "follow_latest": requested_session == "latest"}
                    content = json.dumps(response).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                elif url.path in {"/", "/index.html"}:
                    with open(HTML_PATH, "rb") as handle:
                        content = handle.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                else:
                    self.send_error(404)
                    return
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                self.send_error(502, str(exc))

        def log_message(self, format, *args):
            pass

    print(f"F1 Pit Wall: http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    server = commands.add_parser("serve")
    server.add_argument("--session", default="latest")
    server.add_argument("--pit-loss", type=float, default=22)
    server.add_argument("--port", type=int, default=8000)
    server.add_argument("--replay", help="path to a snapshots.json; serves it instead of live data")
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--session", required=True)
    snapshot.add_argument("--pit-loss", type=float, default=22)
    snapshot.add_argument("--out", required=True)
    snapshots = commands.add_parser("snapshots")
    snapshots.add_argument("--session", required=True)
    snapshots.add_argument("--every", type=int, default=60)
    snapshots.add_argument("--pit-loss", type=float, default=22)
    snapshots.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.command == "serve":
        serve(args.session, args.pit_loss, args.port, args.replay)
    elif args.command == "snapshots":
        states = build_snapshots(fetch_session(args.session), args.every, args.pit_loss)
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(states, handle)
            handle.write("\n")
        print(f"wrote {len(states)} snapshots to {args.out}")
    else:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(build_state(fetch_session(args.session), args.pit_loss), handle, indent=2)
            handle.write("\n")
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
