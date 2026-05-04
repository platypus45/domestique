# Debugging Domestique — backend observability playbook

The v3.6.0-fix30 logging layer is designed so any post-mortem "why did X
happen during the ride?" question can be answered with `grep` + one call to
a debug-snapshot endpoint. No UI, no console drilling — everything is on
disk in a single file per ride.

This doc is the manual: where logs live, which categories exist, how to
flip them up to DEBUG, and a grep recipe for each of the common failure
modes.

## Where logs live

Every ride spawns its own file as soon as `POST /api/training/start`
returns 200. Path template:

```
~/.domestique/logs/ride_<iso_utc_timestamp>_<sessid>.log
```

- `iso_utc_timestamp` is `YYYYMMDDTHHMMSSZ` (e.g. `20260420T103012Z`).
- `sessid` is an 8-char hex tag generated at start and echoed back in the
  debug-snapshot `session_id` field.
- Rotation keeps the **20 most recent** ride logs. Older files are deleted
  on the next `start`. Override with `DOMESTIQUE_RIDE_LOG_KEEP=N`.

The global app log is still at `~/.domestique/logs/domestique.log` (size-
rotated, 30 backups). Per-ride files are a *tee* of the active session —
their rotation is independent of the global one.

A background thread flushes every handler **every ~1 s** so a SIGKILL /
power loss never loses more than a second of log. Nothing extra is needed
at the call sites.

## Named categories

Every ride-critical code path now emits through one of:

| Logger                | Covers                                            |
| --------------------- | ------------------------------------------------- |
| `domestique.ble`      | BLE frames in/out, connect/disconnect, RTL stats  |
| `domestique.gate`     | First-pedal gate decisions, per-tick eval         |
| `domestique.phase`    | ARMED → INDEXING → WARMUP → ROUTE → … transitions |
| `domestique.power`    | Power readings, 4Hz buffer stats, spike filter    |
| `domestique.trainer`  | FTMS 0x11 / 0x05 dispatches, FE-C writes          |
| `domestique.hr`       | HR frames, RR intervals, DFA α1 status            |
| `domestique.session`  | start / stop / pause / resume                     |
| `domestique.ws`       | WS connect / disconnect / tick errors             |

Each log line includes the category name in brackets so `grep` filters
trivially:

```
2026-04-20 10:30:15 INFO  [domestique.gate] [SESSION a1b2c3d4] EVENT=gate_decision ...
```

The `[SESSION ...]` prefix only appears in the per-ride file.

## Enabling verbose output

Three ways, pick whichever fits the situation:

### 1. Environment — whole run

```bash
# everything at DEBUG
DOMESTIQUE_VERBOSE=1 python3.12 app.py

# only specific categories
DOMESTIQUE_LOG_CATEGORIES=gate,ble python3.12 app.py
```

Category names are the short form (`gate`, not `domestique.gate`).
Unknown tokens are silently ignored.

### 2. Runtime — hot-swap via HTTP

```bash
# bump the first-pedal gate to DEBUG without restarting
curl -X POST -H 'Content-Type: application/json' \
  -d '{"level":"DEBUG","category":"gate"}' \
  localhost:8765/api/training/log-level

# current levels
curl -s localhost:8765/api/training/log-level | jq
```

Bodies: `{"level":"DEBUG|INFO|WARNING|ERROR", "category":"gate|ble|..."}`
— omit `category` to move the root logger. File handlers stay at DEBUG
regardless so on-disk logs are always lossless; only the console handler
follows the root level.

### 3. Debug snapshot — dump every internal state at once

```bash
curl -s localhost:8765/api/training/debug-snapshot | jq
```

Returns a single JSON doc with:

- `session_id`, `phase`, `ride_phase_enum`, `elapsed_s`, `paused`, `stop_reason`
- `first_pedal_gate` — streak, prev-tick power/cadence, the three constant
  thresholds, whether cadence has ever been seen.
- `last_trainer_data` — power/cadence/speed/hr + age in seconds.
- `power_buffer` — current buffer size, tail samples, last-known watts.
- `ble` — tri-state lifecycle per role + RTL stats (count / mean / min / max).
- `recent_ticks` — last 10 tick records (also in `trainer-health`).
- `log_file_path` — absolute path of the active per-ride log.
- `log_levels` — effective level per logger.

Designed so a single dump is enough to reproduce or explain any in-flight
state.

## Grep recipes

Run these against the active ride file (`debug-snapshot.log_file_path`
tells you which one):

```bash
LOG=$(curl -s localhost:8765/api/training/debug-snapshot | jq -r .log_file_path)
```

### "Gate never fires"

```bash
grep 'EVENT=gate_decision' "$LOG" | tail -20
```

Each line shows `tick=... phase=ARMED power=… cadence=… prev=(pp,pc)
streak=… rising=… low=… no_cad=… fired=…`. If `fired=false` for every
tick, check which of the three subconditions stayed false: `rising`,
`low`, or `no_cad`. The `prev=(pp,pc)` columns reveal whether the
rising-edge detector was armed at all.

### "BLE not flowing"

```bash
grep 'EVENT=ble_frame' "$LOG" | head -20    # need DEBUG
grep 'EVENT=ble_connect\|EVENT=ble_disconnect' "$LOG"
```

If `ble_frame` returns nothing, confirm the category is at DEBUG
(`curl … log-level` or `DOMESTIQUE_LOG_CATEGORIES=ble`). The
`ble_connect` / `ble_disconnect` pair tells you whether BLE flapped
during the ride.

### "Phase stuck"

```bash
grep 'EVENT=phase_transition' "$LOG"
```

Every ARMED → INDEXING → ROUTE transition emits a single line with
`from=… to=… trigger=… elapsed=…`. An absent `ARMED → INDEXING` means
the first-pedal gate never fired; an absent `INDEXING → ROUTE` means
the 3 s countdown never completed (rare — check `_indexing_elapsed`
in the snapshot).

### "ERG not responding"

```bash
grep '\[domestique.trainer\]' "$LOG" | tail -50
```

Shows every FTMS 0x11 / 0x05 dispatch, ERG engage/disengage, and the
control-point rejections. Pair with the `ERG IDLE -> ACTIVE` /
`ERG target:` log lines.

### "Session lifecycle" (when did start/stop/pause happen?)

```bash
grep 'EVENT=session_' "$LOG"
```

Emits `session_start`, `session_pause`, `session_resume`,
`session_stop`, `session_autoend`. `elapsed=` on each line pins the
wall-clock offset inside the ride.

### "RTL stats — is my BLE link good?"

Every 60 s the manager logs `FTMS RTL Stats: device=... count=N min=...
max=... mean=... stddev=... success-ratio=...` per subscription. In
the snapshot, `ble.rtl_stats_last_60s` has the same numbers. Spikes in
`max` or `stddev` above ~0.5 s indicate a flaky link even when the
connection didn't drop.

## Structured event schema

All `EVENT=...` emissions are single-line key=value pairs, parseable
with any shell tool. The stable event types are:

| Event                | Fields                                                           |
| -------------------- | ---------------------------------------------------------------- |
| `gate_decision`      | `tick`, `phase`, `power`, `cadence`, `prev=(pp,pc)`, `streak`, `rising`, `low`, `no_cad`, `fired` |
| `phase_transition`   | `from`, `to`, `trigger`, `elapsed`                               |
| `session_start`      | `mode`, `ftp`, `course`, `workout`                               |
| `session_http_start` | `session_id`, `mode`, `course`, `workout`                        |
| `session_pause`      | `reason`, `elapsed`, `phase`                                     |
| `session_resume`     | `reason`, `elapsed`, `phase`                                     |
| `session_stop`       | `elapsed`, `active_seconds`, `phase`, `reason`                   |
| `session_autoend`    | `reason`, `elapsed`                                              |
| `ble_connect`        | `role`, `proto`, `name`, `addr`                                  |
| `ble_disconnect`     | `role`, `name`, `addr`                                           |
| `ble_frame`          | `role`, `power`, `cadence`, `speed`, `hr`, `mono` (DEBUG only)   |
| `ws_connect`         | `origin`                                                         |
| `ws_disconnect`      | `reason`                                                         |
| `ws_reject`          | `origin`, `reason`                                               |
| `ws_error`           | `reason`, `msg`                                                  |

When adding a new emission, follow the same shape so the analysis
scripts never need a second parser.

## First triage step for any new bug report

Start here:

```bash
# 1. Snapshot the live state (one-shot — doesn't block the ride)
curl -s localhost:8765/api/training/debug-snapshot > /tmp/snap.json
jq . /tmp/snap.json

# 2. Find the ride log
LOG=$(jq -r .log_file_path /tmp/snap.json)
ls -la "$LOG"

# 3. Bump relevant category to DEBUG if the bug is reproducible
curl -X POST -H 'Content-Type: application/json' \
  -d '{"level":"DEBUG","category":"gate"}' \
  localhost:8765/api/training/log-level

# 4. Grep the event of interest
grep 'EVENT=' "$LOG" | tail -50
```

If `log_file_path` is null, no session is active — start one and try
again.
