# F1 Pit Wall

See the whole race: running order, gaps, tyre life, recent pace, race control, and the field each
driver would rejoin after a stop. The interface runs locally in one browser tab; the server is one
Python file with no third-party packages.

[Open the 2025 Monaco Grand Prix replay.](https://josephbsmith.com/pit-wall)

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

The rejoin column is intentionally simple: current gap to the leader plus the configured pit-loss
time, ranked back into the current field. It is a decision aid, not a strategy prediction.

## Check

```sh
python3 test_pitwall.py
```

Data from [OpenF1](https://openf1.org/docs/). No licensed video is stored or rebroadcast. Not
affiliated with Formula 1. MIT licensed.
