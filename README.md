# F1 Pit Wall

Local, zero-dependency OpenF1 workstation for every session in a race weekend.

[Open the 2025 Monaco Grand Prix replay.](https://josephbsmith.com/pit-wall)

## Views

- **Timing** — live race order or best-lap classification for practice and qualifying, including Q1/Q2/Q3 results.
- **Pace** — best and recent laps, sectors, mini-sectors, speed traps, deltas, and lap trends.
- **Strategy** — tyre stints, tyre age, pit and lane times, and an adjustable rejoin estimate.
- **Track** — recent car locations plus speed, RPM, gear, throttle, brake, and DRS state.
- **Feed** — race control, flags, overtakes, pit events, and available team-radio audio.
- **Standings** — driver and constructor championship movement for race sessions.

The session rail loads practice, sprint, qualifying, and race sessions from the same meeting.

## Run live

```sh
python3 pitwall.py serve --session latest --pit-loss 22 --port 8000
```

Open `http://localhost:8000`. Timing refreshes every ten seconds. Real-time [OpenF1](https://openf1.org) data requires a subscribed account:

```sh
export OPENF1_USERNAME="your-email"
export OPENF1_PASSWORD="your-password"
python3 pitwall.py serve --session latest
```

The app exchanges those credentials for OpenF1's one-hour access token and refreshes it automatically. A temporary `OPENF1_TOKEN` is still accepted if you already have one.

Historical sessions use their numeric session key and are available without authentication.

## Bake a replay

```sh
python3 pitwall.py snapshots --session 9979 --every 120 --out monaco.json
python3 pitwall.py serve --replay monaco.json
```

The rejoin estimate is current gap to the leader plus the configured pit-loss time, ranked back
into the current field. Change the assumption directly in the interface.

## Check

```sh
python3 test_pitwall.py
```

Data from [OpenF1](https://openf1.org/docs/). No licensed video is stored or rebroadcast. Not
affiliated with Formula 1. MIT licensed.
