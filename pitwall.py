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
    """State as known at time t: drop records dated after t.

    OpenF1 stints have no timestamp, so they are gated by each driver's latest
    known lap instead.
    """
    cutoff = t if isinstance(t, datetime) else datetime.fromisoformat(t)
    filtered = {
        endpoint: [r for r in records if (d := _record_date(r)) is None or d <= cutoff]
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
        "lap_number": max((row.get("lap_number", 0) for row in data.get("laps", [])), default=0),
        "pit_loss_seconds": pit_loss,
        "board": board,
        "weather": weather,
        "race_control": messages,
    }


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e10600">
<title>F1 Pit Wall</title>
<style>
:root{--red:#e10600;--ink:#111217;--panel:#1a1c22;--panel2:#22252d;--line:#343741;--white:#f7f4ef;--muted:#999da8;--green:#b6f238;--cyan:#36c5f0;--purple:#c58cff;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--sans:Arial,Helvetica,sans-serif}
*{box-sizing:border-box}html{background:var(--ink);color-scheme:dark}body{margin:0;background:var(--ink);color:var(--white);font-family:var(--sans);min-height:100vh}.topbar{height:74px;background:var(--red);display:flex;align-items:center;justify-content:space-between;padding:0 clamp(16px,3vw,48px);position:relative;overflow:hidden}.topbar:after{content:"";position:absolute;inset:0 0 0 58%;background:radial-gradient(circle,rgba(0,0,0,.28) 1.2px,transparent 1.5px);background-size:8px 8px;transform:skewX(-18deg);transform-origin:bottom}.brand{display:flex;align-items:center;gap:13px;position:relative;z-index:1}.brand-mark{width:42px;height:23px;border-top:7px solid #fff;border-right:7px solid #fff;transform:skewX(-24deg)}.brand strong{font-size:18px;font-style:italic;letter-spacing:-.03em}.top-status{position:relative;z-index:1;display:flex;align-items:center;gap:9px;font:700 11px var(--mono);letter-spacing:.1em}.signal{width:8px;height:8px;background:#fff;border-radius:50%;box-shadow:0 0 0 4px rgba(255,255,255,.18)}
.workspace{width:min(1480px,100%);margin:auto;padding:clamp(18px,3vw,42px)}.session-head{display:flex;align-items:end;justify-content:space-between;gap:24px;margin-bottom:22px}.eyebrow{display:block;color:var(--red);font:800 11px var(--mono);letter-spacing:.16em;text-transform:uppercase;margin-bottom:6px}.session-head h1{margin:0;font:900 italic clamp(34px,5vw,64px)/.88 var(--sans);letter-spacing:-.065em;text-transform:uppercase}.session-head h1 span{color:transparent;-webkit-text-stroke:1px var(--white)}.meta{margin:0;color:var(--muted);font:600 11px/1.6 var(--mono);letter-spacing:.06em;text-align:right;text-transform:uppercase}
.session-strip{min-height:72px;background:var(--red);display:grid;grid-template-columns:minmax(180px,1.4fr) repeat(3,minmax(100px,.7fr));align-items:stretch;clip-path:polygon(0 0,100% 0,100% calc(100% - 16px),calc(100% - 16px) 100%,0 100%)}.session-strip>div{padding:15px 20px;border-right:1px solid rgba(255,255,255,.22)}.session-strip small{display:block;font:700 9px var(--mono);letter-spacing:.16em;opacity:.72;text-transform:uppercase;margin-bottom:7px}.session-strip strong{display:block;font:900 18px/1 var(--sans);letter-spacing:-.025em;text-transform:uppercase}.session-strip .race-name strong{font-size:22px;font-style:italic}
#transport{display:none;align-items:center;gap:16px;background:#0b0c0f;border:1px solid var(--line);border-top:0;padding:12px 16px}#transport.on{display:flex}#play{width:38px;height:34px;border:0;background:var(--white);color:var(--ink);font:900 15px var(--sans);cursor:pointer;clip-path:polygon(0 0,100% 0,100% calc(100% - 8px),calc(100% - 8px) 100%,0 100%)}#play:hover{background:var(--red);color:#fff}#scrub{flex:1;accent-color:var(--red)}#tick{min-width:112px;text-align:right;color:var(--muted);font:700 10px var(--mono);letter-spacing:.08em}
.timing-grid{display:grid;grid-template-columns:minmax(0,2.2fr) minmax(280px,.8fr);gap:12px;margin-top:12px}.panel{background:var(--panel);border-top:3px solid var(--red);min-width:0}.panel-title{height:46px;display:flex;align-items:center;justify-content:space-between;padding:0 16px;border-bottom:1px solid var(--line);font:800 11px var(--mono);letter-spacing:.13em;text-transform:uppercase}.panel-title span{color:var(--muted);font-size:9px}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:850px;font-variant-numeric:tabular-nums}th{height:34px;padding:0 10px;background:#0d0e12;color:#848895;text-align:left;font:700 9px var(--mono);letter-spacing:.12em;text-transform:uppercase}td{height:56px;padding:6px 10px;border-top:1px solid var(--line);font:700 12px var(--mono);white-space:nowrap}tbody tr{background:var(--panel)}tbody tr:hover{background:var(--panel2)}.pos{width:44px;color:#fff;font:900 italic 20px var(--sans);text-align:center}.driver-cell{display:flex;align-items:center;gap:10px;border-left:4px solid;padding-left:10px}.driver-no{color:var(--muted);font-size:9px}.driver-name{display:block;color:#fff;font:900 italic 16px var(--sans);letter-spacing:-.025em}.driver-team{display:block;max-width:130px;overflow:hidden;text-overflow:ellipsis;color:var(--muted);font:600 8px var(--mono);letter-spacing:.06em;text-transform:uppercase;margin-top:2px}.leader{color:var(--purple)}.tyre{display:inline-flex;align-items:center;gap:6px}.tyre-dot{width:18px;height:18px;border:3px solid var(--tyre);border-radius:50%;display:inline-grid;place-items:center;color:var(--tyre);font:900 8px var(--sans)}.tyre-age{color:var(--muted);font-size:9px}.pace{color:var(--green)}.spark{color:var(--green);vertical-align:middle}.spark-grid{stroke:#444852;stroke-width:.7}.rejoin{font:900 15px var(--sans)}.loss{display:block;color:var(--red);font:700 8px var(--mono);letter-spacing:.04em;margin-top:2px}.hold{color:var(--muted)}.stint-history{display:flex;gap:4px}.stint{width:18px;height:18px;border:2px solid var(--tyre);border-radius:50%;display:grid;place-items:center;color:var(--tyre);font:800 7px var(--sans)}
.side{display:grid;align-content:start;gap:12px}.weather-grid{display:grid;grid-template-columns:1fr 1fr}.weather-stat{min-height:78px;padding:14px 15px;border-bottom:1px solid var(--line);border-right:1px solid var(--line)}.weather-stat:nth-child(even){border-right:0}.weather-stat:nth-last-child(-n+2){border-bottom:0}.weather-stat small{display:block;color:var(--muted);font:700 8px var(--mono);letter-spacing:.13em;text-transform:uppercase;margin-bottom:8px}.weather-stat strong{font:900 20px var(--mono)}.weather-stat .unit{color:var(--muted);font-size:10px;margin-left:3px}.messages{list-style:none;margin:0;padding:0;max-height:510px;overflow:auto}.messages li{display:grid;grid-template-columns:44px 1fr;gap:9px;padding:12px 14px;border-top:1px solid var(--line);font:600 10px/1.45 var(--mono)}.messages li:first-child{border-top:0}.lap-tag{align-self:start;background:var(--red);color:white;padding:4px 5px;text-align:center;font-size:8px;font-weight:800}.message-type{display:block;color:var(--muted);font-size:8px;letter-spacing:.1em;margin-bottom:3px;text-transform:uppercase}.empty{padding:28px 16px!important;color:var(--muted)}
.legend{display:flex;flex-wrap:wrap;gap:16px;padding:16px 2px;color:var(--muted);font:600 9px var(--mono);letter-spacing:.06em;text-transform:uppercase}.legend b{color:var(--white)}.legend i{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--red);margin-right:6px}.error{color:#fff;background:var(--red);padding:10px 14px}
@media(max-width:980px){.timing-grid{grid-template-columns:1fr}.side{grid-template-columns:1fr 1fr}.messages{max-height:320px}}
@media(max-width:700px){.topbar{height:60px}.workspace{padding:16px 10px 30px}.session-head{align-items:start}.session-head h1{font-size:38px}.meta{display:none}.session-strip{grid-template-columns:1.5fr 1fr}.session-strip>div:nth-child(3),.session-strip>div:nth-child(4){display:none}.session-strip .race-name strong{font-size:17px}#transport{gap:9px;padding:10px}#tick{min-width:74px;font-size:8px}.side{grid-template-columns:1fr}table{min-width:0;table-layout:fixed}th,td{padding-left:4px;padding-right:4px}.optional{display:none}th:first-child{width:36px}.pos{width:36px;font-size:16px}.driver-col{width:76px}.driver-cell{gap:4px;padding-left:6px}.driver-name{font-size:13px}.driver-team,.driver-no{display:none}.gap-col{width:64px}.tyre-col{width:58px}.pace-col{width:72px}.rejoin-col{width:54px}.tyre-age{font-size:8px}.legend{gap:10px}.messages{max-height:none}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
</style>
</head>
<body>
<header class="topbar"><div class="brand"><span class="brand-mark" aria-hidden="true"></span><strong>PIT WALL</strong></div><div class="top-status"><span class="signal"></span><span id="mode">CONNECTING</span></div></header>
<main class="workspace">
  <div class="session-head"><div><span class="eyebrow">OpenF1 race intelligence</span><h1>See the <span>whole race.</span></h1></div><p class="meta" id="meta" aria-live="polite">Waiting for timing feed…</p></div>
  <section class="session-strip" aria-label="Session status">
    <div class="race-name"><small>Session</small><strong id="sessionName">Pit Wall</strong></div>
    <div><small>Race status</small><strong id="lapNow">—</strong></div>
    <div><small>Pit loss model</small><strong id="pitLoss">—</strong></div>
    <div><small>Last update</small><strong id="lastUpdate">—</strong></div>
  </section>
  <div id="transport"><button id="play" aria-label="Play replay">▶</button><input id="scrub" type="range" min="0" max="0" value="0" aria-label="Replay position"><span id="tick"></span></div>
  <div class="timing-grid">
    <section class="panel"><div class="panel-title">Timing tower <span>Gap + strategy model</span></div><div class="table-wrap"><table><caption hidden>Current running order and strategy data</caption><thead><tr><th>Pos</th><th class="driver-col">Driver</th><th class="gap-col">Gap</th><th class="optional">Interval</th><th class="tyre-col">Tyre</th><th class="pace-col">Last 3</th><th class="optional">Pace</th><th class="rejoin-col">Rejoin</th><th class="optional">Stints</th></tr></thead><tbody id="board"><tr><td class="empty" colspan="9">ACQUIRING TIMING DATA…</td></tr></tbody></table></div></section>
    <aside class="side">
      <section class="panel"><div class="panel-title">Track conditions <span>Latest</span></div><div class="weather-grid"><div class="weather-stat"><small>Air</small><strong id="air">—</strong><span class="unit">°C</span></div><div class="weather-stat"><small>Track</small><strong id="track">—</strong><span class="unit">°C</span></div><div class="weather-stat"><small>Wind</small><strong id="wind">—</strong><span class="unit">m/s</span></div><div class="weather-stat"><small>Rain</small><strong id="rain">—</strong><span class="unit"></span></div></div></section>
      <section class="panel"><div class="panel-title">Race control <span>Latest messages</span></div><ol class="messages" id="messages"><li class="empty">NO MESSAGES</li></ol></section>
    </aside>
  </div>
  <footer class="legend"><span><i></i><b>Rejoin</b>&nbsp; = current gap + pit loss</span><span><b>Last 3</b>&nbsp; = median lap time</span><span>Local display · no video feed</span></footer>
</main>
<script>
const $=id=>document.getElementById(id);
const elements={meta:$('meta'),mode:$('mode'),sessionName:$('sessionName'),lapNow:$('lapNow'),pitLoss:$('pitLoss'),lastUpdate:$('lastUpdate'),transport:$('transport'),play:$('play'),scrub:$('scrub'),tick:$('tick'),board:$('board'),messages:$('messages'),air:$('air'),track:$('track'),wind:$('wind'),rain:$('rain')};
const TYRE={SOFT:'#ff1e20',MEDIUM:'#ffd12e',HARD:'#f4f4f4',INTERMEDIATE:'#3acb5a',WET:'#2f8cff'};
const show=v=>v===null||v===undefined||v===''?'—':v;
const esc=v=>String(show(v)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number=v=>v===null||v===undefined||v===''?Number.NaN:typeof v==='number'?v:Number(v);
const gap=(v,leader=false)=>{if(leader)return 'LEADER';const n=number(v);return Number.isFinite(n)?n===0?'—':`+${n.toFixed(3)}`:show(v)};
const lapTime=v=>{if(!Number.isFinite(v))return '—';const minutes=Math.floor(v/60);return `${minutes}:${(v-minutes*60).toFixed(3).padStart(6,'0')}`};
const clock=v=>{const d=new Date(v);return Number.isNaN(d.valueOf())?'—':d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})};
const compound=v=>v==='INTERMEDIATE'?'I':v==='WET'?'W':v?String(v)[0]:'?';
function currentLap(s){return s.lap_number||Math.max(0,...(s.race_control||[]).map(m=>Number(m.lap_number)||0))}
function strategyAt(r,lap,precise){
  if(precise||!lap||!r.stint_history?.length)return {compound:r.compound,age:r.tyre_age_laps,history:r.stint_history||[]};
  let remaining=lap;const history=[];
  for(const stint of r.stint_history){const length=Number(stint.laps)||0;if(remaining<=0)break;const used=Math.min(length,remaining);history.push({...stint,laps:used});if(remaining<=length)return {compound:stint.compound,age:used,history};remaining-=length}
  return {compound:r.compound,age:r.tyre_age_laps,history};
}
function render(s,replay=false){
  elements.mode.textContent=replay?'REPLAY':'LIVE DATA';
  elements.sessionName.textContent=replay?'Session replay':'Live session';
  elements.lapNow.textContent=currentLap(s)?`LAP ${currentLap(s)}`:'SESSION';
  elements.pitLoss.textContent=`${show(s.pit_loss_seconds)} SEC`;
  elements.lastUpdate.textContent=replay?'ARCHIVE':clock(s.generated_utc);
  elements.meta.textContent=replay?'Archived session · scrub or play':`Updated ${clock(s.generated_utc)} · refreshes every 10 seconds`;
  const w=s.weather||{};
  elements.air.textContent=show(w.air_temperature);elements.track.textContent=show(w.track_temperature);elements.wind.textContent=show(w.wind_speed);elements.rain.textContent=w.rainfall===undefined?'—':w.rainfall?'YES':'NO';
  elements.board.innerHTML=s.board.length?s.board.map(r=>{
    const colour=String(r.team_colour||'777777').replace(/[^0-9a-f]/gi,'').slice(0,6)||'777777';
    const strategy=strategyAt(r,currentLap(s),Boolean(s.lap_number));
    const tyre=TYRE[strategy.compound]||'#777b85';
    const spark=r.pace_sparkline_path?`<svg class="spark" width="66" height="22" viewBox="0 0 60 20" aria-label="Five-lap pace trend"><path class="spark-grid" d="M0 10H60"/><path d="${r.pace_sparkline_path}" fill="none" stroke="currentColor" stroke-width="1.8" vector-effect="non-scaling-stroke"/></svg>`:'—';
    const stints=strategy.history.map(t=>{const c=TYRE[t.compound]||'#777b85';return `<span class="stint" style="--tyre:${c}" title="${esc(t.compound)} · ${esc(t.laps)} laps">${esc(compound(t.compound))}</span>`}).join('')||'—';
    const loss=Number(r.estimated_rejoin_position)-Number(r.position);
    const rejoin=r.estimated_rejoin_position?`<span class="rejoin">P${esc(r.estimated_rejoin_position)}</span><span class="${loss>0?'loss':'loss hold'}">${loss>0?`−${loss} POS`:'HOLD'}</span>`:'—';
    return `<tr><td class="pos">${esc(r.position)}</td><td><div class="driver-cell" style="border-color:#${colour}"><span class="driver-no">${esc(r.driver_number)}</span><span><span class="driver-name">${esc(r.name)}</span><span class="driver-team">${esc(r.team)}</span></span></div></td><td class="${r.position===1?'leader':''}">${esc(gap(r.gap_to_leader,r.position===1))}</td><td class="optional">${esc(gap(r.interval))}</td><td><span class="tyre"><span class="tyre-dot" style="--tyre:${tyre}">${esc(compound(strategy.compound))}</span><span class="tyre-age">${strategy.age===null||strategy.age===undefined?'—':`${esc(strategy.age)} L`}</span></span></td><td class="pace">${esc(lapTime(r.recent_pace_seconds))}</td><td class="optional">${spark}</td><td>${rejoin}</td><td class="optional"><div class="stint-history">${stints}</div></td></tr>`
  }).join(''):'<tr><td class="empty" colspan="9">NO TIMING DATA YET</td></tr>';
  const control=s.race_control||[];
  elements.messages.innerHTML=control.length?[...control].reverse().map(m=>`<li><span class="lap-tag">L${esc(m.lap_number)}</span><span><span class="message-type">${esc(m.category||m.flag||'Race control')}</span>${esc(m.message)}</span></li>`).join(''):'<li class="empty">NO RACE CONTROL MESSAGES</li>';
}
async function state(){const r=await fetch('/state.json');if(!r.ok)throw Error(`${r.status} ${r.statusText}`);return r.json()}
async function load(){try{render(await state())}catch(e){elements.meta.classList.add('error');elements.meta.textContent=`Feed unavailable · ${e.message}`}}
async function boot(){
  try{const r=await fetch('/snapshots.json');if(r.ok){const snaps=await r.json();const start=snaps.findIndex(s=>currentLap(s)>=10&&s.board.some(r=>r.recent_pace_seconds));let i=start<0?0:start;let timer=null;elements.transport.classList.add('on');elements.scrub.max=snaps.length-1;
    const stop=()=>{clearInterval(timer);timer=null;elements.play.textContent='▶';elements.play.setAttribute('aria-label','Play replay')};
    const draw=()=>{render(snaps[i],true);elements.scrub.value=i;elements.tick.textContent=`FRAME ${String(i+1).padStart(2,'0')} / ${String(snaps.length).padStart(2,'0')}`};
    elements.scrub.oninput=()=>{i=Number(elements.scrub.value);stop();draw()};
    elements.play.onclick=()=>{if(timer)return stop();elements.play.textContent='Ⅱ';elements.play.setAttribute('aria-label','Pause replay');timer=setInterval(()=>{if(i>=snaps.length-1)return stop();i++;draw()},900)};
    draw();return}}
  catch(e){}
  load();setInterval(load,10000)
}
boot();
</script>
</body>
</html>"""


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
