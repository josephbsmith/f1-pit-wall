#!/usr/bin/env python3
"""Local OpenF1 race-state dashboard."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


BASE = "https://api.openf1.org/v1"
ENDPOINTS = ("drivers", "position", "intervals", "laps", "stints", "race_control", "weather")


def fetch(endpoint: str, session: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"session_key": session})
    request = urllib.request.Request(f"{BASE}/{endpoint}?{query}", headers={"User-Agent": "F1-Pit-Wall/1.0"})
    token = os.environ.get("OPENF1_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 3:
                raise
            time.sleep(2 ** attempt * 5)  # 5s, 10s, 20s — OpenF1 free tier rate limit
    raise RuntimeError("unreachable")


def fetch_session(session: str) -> dict[str, list[dict[str, Any]]]:
    with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as pool:
        values = pool.map(lambda endpoint: fetch(endpoint, session), ENDPOINTS)
    return dict(zip(ENDPOINTS, values))


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


def build_state_at(data: dict[str, list[dict[str, Any]]], t: str | datetime, pit_loss: float = 22.0) -> dict[str, Any]:
    """State as known at time t: drop records dated after t (undated records stay).

    ponytail: stints carry no date field on OpenF1, so a driver's future
    compound leaks into early snapshots; cross-referencing stint lap_start
    against lap date_start would fix it if replay fidelity ever matters.
    """
    cutoff = t if isinstance(t, datetime) else datetime.fromisoformat(t)
    filtered = {
        endpoint: [r for r in records if (d := _record_date(r)) is None or d <= cutoff]
        for endpoint, records in data.items()
    }
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


def sparkline_path(values: list[float], width: int = 60, height: int = 20) -> str:
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
    drivers = {row["driver_number"]: row for row in data.get("drivers", [])}
    positions = latest(data.get("position", []), "driver_number")
    intervals = latest(data.get("intervals", []), "driver_number")
    current_stints = latest(data.get("stints", []), "driver_number", "stint_number")
    laps_by_driver: dict[int, list[dict[str, Any]]] = {}
    for lap in data.get("laps", []):
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
    board = []
    for number, position in positions.items():
        driver_laps = sorted(laps_by_driver.get(number, []), key=lambda row: row.get("lap_number", 0))
        valid = [float(row["lap_duration"]) for row in driver_laps if isinstance(row.get("lap_duration"), (int, float))]
        recent_pace = statistics.median(valid[-3:]) if valid else None
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
        board.append(
            {
                "position": position.get("position"),
                "driver_number": number,
                "name": profile.get("name_acronym") or profile.get("full_name") or str(number),
                "team": profile.get("team_name"),
                "team_colour": profile.get("team_colour"),
                "gap_to_leader": intervals.get(number, {}).get("gap_to_leader", 0 if leader and number == leader["driver_number"] else None),
                "interval": intervals.get(number, {}).get("interval"),
                "compound": stint.get("compound"),
                "tyre_age_laps": tyre_age,
                "recent_pace_seconds": recent_pace,
                "estimated_rejoin_position": projected,
                "pace_sparkline_path": sparkline_path(valid[-5:]),
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
    messages = sorted(data.get("race_control", []), key=lambda row: str(row.get("date", "")))[-8:]
    return {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "pit_loss_seconds": pit_loss,
        "board": board,
        "weather": weather,
        "race_control": messages,
    }


HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>
<title>F1 Pit Wall</title><style>body{font:15px system-ui;background:#101114;color:#eee;margin:1rem}h1{margin:.2rem 0}table{border-collapse:collapse;width:100%}th,td{padding:.45rem;border-bottom:1px solid #333;text-align:left}.muted{color:#aaa}#messages li{margin:.35rem 0}.team{border-left:5px solid;padding-left:.4rem}
.spark{color:#8ab4f8}.stintbar{display:flex;width:90px;height:10px;border-radius:2px;overflow:hidden}.stintbar span{display:block}
#transport{display:none;align-items:center;gap:10px;margin:.6rem 0}#transport.on{display:flex}#transport button{background:#222;border:1px solid #444;color:#eee;padding:.3rem .7rem;border-radius:4px;cursor:pointer}#transport input{flex:1}</style>
<h1>F1 Pit Wall</h1><p id=meta class=muted>Loading…</p>
<div id=transport><button id=play>Play</button><input id=scrub type=range min=0 max=0 value=0><span id=tick class=muted></span></div>
<div id=weather></div>
<table><thead><tr><th>P</th><th>Driver</th><th>Gap</th><th>Interval</th><th>Tyre</th><th>Age</th><th>Stints</th><th>Pace trend</th><th>Last-3 pace</th><th>Rejoin</th></tr></thead><tbody id=board></tbody></table>
<h2>Race control</h2><ul id=messages></ul><script>
const show=v=>v===null||v===undefined?'—':v;
const esc=v=>String(show(v)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const TYRE={SOFT:'#e33',MEDIUM:'#fc3',HARD:'#eee',INTERMEDIATE:'#3a3',WET:'#36c'};
function render(s){
meta.textContent=`Updated ${s.generated_utc} · pit-loss assumption ${s.pit_loss_seconds}s`;
weather.textContent=s.weather.air_temperature===undefined?'':`Air ${s.weather.air_temperature}°C · Track ${s.weather.track_temperature}°C · Rain ${s.weather.rainfall}`;
board.innerHTML=s.board.map(r=>{const colour=String(r.team_colour||'777').replace(/[^0-9a-f]/gi,'').slice(0,6)||'777';
const spark=r.pace_sparkline_path?`<svg class=spark width=60 height=20 viewBox="0 0 60 20"><path d="${r.pace_sparkline_path}" fill=none stroke=currentColor stroke-width=1.5/></svg>`:'—';
const stints=(r.stint_history&&r.stint_history.length)?`<div class=stintbar>${r.stint_history.map(t=>`<span style="flex:${t.laps};background:${TYRE[t.compound]||'#777'}"></span>`).join('')}</div>`:'—';
return `<tr><td>${esc(r.position)}</td><td><span class=team style="border-color:#${colour}">${esc(r.name)}</span></td><td>${esc(r.gap_to_leader)}</td><td>${esc(r.interval)}</td><td>${esc(r.compound)}</td><td>${esc(r.tyre_age_laps)}</td><td>${stints}</td><td>${spark}</td><td>${r.recent_pace_seconds?esc(r.recent_pace_seconds.toFixed(3)):'—'}</td><td>${r.estimated_rejoin_position?'P'+esc(r.estimated_rejoin_position):'—'}</td></tr>`}).join('');
messages.innerHTML=s.race_control.map(m=>`<li>L${esc(m.lap_number)} · ${esc(m.message)}</li>`).join('');}
async function load(){try{render(await fetch('/state.json').then(r=>{if(!r.ok)throw Error(r.statusText);return r.json()}))}catch(e){meta.textContent='Update failed: '+e.message}}
async function boot(){
try{const r=await fetch('/snapshots.json');
if(r.ok){const snaps=await r.json();let i=0,timer=null;
transport.classList.add('on');scrub.max=snaps.length-1;
const draw=()=>{render(snaps[i]);scrub.value=i;tick.textContent=`${i+1}/${snaps.length}`};
scrub.oninput=()=>{i=+scrub.value;draw()};
const stop=()=>{clearInterval(timer);timer=null;play.textContent='Play'};
play.onclick=()=>{if(timer)return stop();play.textContent='Pause';timer=setInterval(()=>{if(i>=snaps.length-1)return stop();i++;draw()},700)};
draw();
if(!matchMedia('(prefers-reduced-motion: reduce)').matches)play.click();
return}}catch(e){}
load();setInterval(load,10000)}
boot();</script>"""


def serve(session: str, pit_loss: float, port: int, replay: str | None = None) -> None:
    replay_bytes = None
    if replay:
        with open(replay, "rb") as handle:
            replay_bytes = handle.read()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                if self.path == "/snapshots.json" and replay_bytes is not None:
                    content = replay_bytes
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                elif self.path == "/state.json" and replay_bytes is None:
                    content = json.dumps(build_state(fetch_session(session), pit_loss)).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                elif self.path in {"/", "/index.html"}:
                    content = HTML.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                else:
                    self.send_error(404)
                    return
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
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
