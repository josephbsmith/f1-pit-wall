# F1 Pit Wall

A local, read-only second screen for an OpenF1 race session. It keeps position, gaps, tyre compound and
age, recent pace, estimated pit rejoin position, latest weather, and race-control messages visible in
one browser tab.

```sh
python3 pitwall.py serve --session latest --pit-loss 22 --port 8000
```

Then open `http://localhost:8000`. The page refreshes every ten seconds. Historical sessions use their
numeric session key. If the selected OpenF1 access tier requires authentication, set `OPENF1_TOKEN` in
the shell before starting the server.

The implementation follows the [OpenF1 API documentation](https://openf1.org/docs/). The pit-rejoin
column is a transparent static estimate: current gap to leader plus the configured pit-loss seconds,
ranked against the field's current gaps. It is not a strategy prediction. This repository stores no
licensed video and does not rebroadcast an OpenF1 data feed.

[Replay the public demonstration.](https://josephbsmith.com/pit-wall)

## Check

```sh
python3 test_pitwall.py
```

MIT licensed.
