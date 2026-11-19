# Valve Control

<img src="https://doover.com/wp-content/uploads/Doover-Logo-Landscape-Navy-padded-small.png" alt="App Icon" style="max-width: 300px;">

**Open and close on/off valves and gates from the device page**

[![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)](https://github.com/getdoover/valve-control)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/getdoover/valve-control/blob/main/LICENSE)

[Getting Started](#getting-started) • [Configuration](#configuration) • [Channels](#channels) • [Need Help?](#need-help)

<br/>

## Overview

A remote control for one or more on/off valves, sluice or flood gates, or any other
output with two states. It runs in the cloud and works with any edge controller that
reports the valve's state on a channel and acts on commands from another: a Doovit app,
a PLC bridge, a LoRaWAN node.

The edge controller is the source of truth. It carries out each command, runs any local
logic (timed closes, schedules, interlocks) and reports what the valve is actually doing.
So the page always shows the **reported** state. A command shows as pending until the
valve reports the commanded state, or is flagged once the confirmation timeout passes.

Key capabilities:

- **Several valves per install** -- each with its own panel, target id and inputs
- **Timed or latched** -- timed valves open for a chosen duration and the edge closes them; latched valves stay open until closed
- **Pending and confirmation** -- every command waits for the reported state to agree, with a warning if it doesn't
- **Countdown** -- a timed open shows when the valve is due to close
- **Notifications** -- an unconfirmed command, and a valve open longer than its maximum
- **Your words** -- the open/closed labels are configurable (e.g. Raised / Lowered)

<br/>

## Getting Started

1. Install **Valve Control** on the device whose channels carry the valves' state.
2. Add a **Valve** for each output: its name, the target id the edge controller knows it
   by, and the message path of its reported state.
3. Point the edge controller at the **Command Channel** (`io_commands`) and the
   **Settings Channel** (`control_settings`) described below.

Until the first reading arrives each valve shows **Unknown**, and nothing is notified.

### Configuration

| Setting | Key | Description | Default |
|---------|-----|-------------|---------|
| **Valves** | `valves` | The valves or gates, one panel each (see below) | *required* |
| **Open Label** | `open_label` | Word for the open state and the open control | `Open` |
| **Closed Label** | `closed_label` | Word for the closed state | `Closed` |
| **Close Label** | `close_label` | Word on the close button | `Close` |
| **Confirmation Timeout (s)** | `confirm_timeout_s` | How long a command may wait for the reported state before it is flagged; 0 never flags | `300` |
| **Command Channel** | `command_channel` | Where commands are published | `io_commands` |
| **Settings Channel** | `settings_channel` | Whose aggregate receives the valves' settings | `control_settings` |
| **Subscription** | `dv_proc_subscriptions` | Channels that invoke the app | `["io_state", "ui_cmds", "io_commands"]` |
| **Schedule** | `dv_proc_schedules` | How often timeouts are checked between readings | `rate(5 minutes)` |

Each **Valve**:

| Setting | Key | Description | Default |
|---------|-----|-------------|---------|
| **Name** | `name` | Shown on the panel, e.g. `Flood Gate 1` | `Valve 1` |
| **Target ID** | `target` | The output id sent in each command's `target` | `valve_1` |
| **State Input Path** | `state_path` | Message path of the reported state, `$channel.path.to.value`. Truthy (`true`, `1`, `"open"`, `"on"`) is open; falsy (`false`, `0`, `"closed"`, `"off"`) is closed | `$io_state.valve_1` |
| **Position Input Path** | `position_path` | Optional message path of the position, 0-100 %. With no state input, a position above 0 counts as open | empty |
| **Mode** | `mode` | `Timed` or `Latched` | `Timed` |
| **Default Open Duration (min)** | `default_open_min` | Timed: the duration offered when opening | `30` |
| **Max Open Duration (min)** | `max_open_min` | The longest a timed open may be, and how long either mode may stay open before a notification; 0 for no limit | `240` |

Readings are taken from messages on the subscribed channels; a message without a valve's
path leaves that valve as it was. Keep every input path's channel in **Subscription**.

### The page

Each valve has a panel with:

- **State** -- Open, Closed, or Unknown before the first reading (also in the panel header)
- **Position** -- when a position input is configured
- **Closes** -- for a confirmed timed open, a countdown to when the edge is due to close it
- **Last Changed** -- when the reported state last changed
- **Last Command** -- the last command and what became of it (sent, accepted, confirmed, rejected, not confirmed)
- **Open For (min)** (Timed) or **Open** (Latched), and **Close**
- A warning line while a command is pending, after one times out or is rejected, when the
  valve has been open longer than its maximum, or when its configuration is incomplete

<br/>

## Channels

### Commands (Command Channel, default `io_commands`)

One message per command:

```json
{"command": "open", "target": "flood_gate_1", "duration_s": 1800, "params": {},
 "request_id": "3f0c1f9e-...", "app_key": "<APP_KEY>", "app": "valve_control",
 "ts": 1795050000000}
```

- `command` is `open` or `close`.
- `duration_s` is the timed open's duration in seconds; `null` for a latched open and for `close`.
- The edge controller may answer on the same channel with
  `{"request_id": "...", "ok": true|false, "error": "..."}`. A rejection is shown on the
  panel; an acceptance is noted. An answer is never required: the reported state is what
  confirms a command.

### Settings (Settings Channel, default `control_settings`)

On every deployment (and so every config change) the app writes its valves to the
channel's aggregate under its own app key, leaving other apps' keys alone:

```json
{"<APP_KEY>": {"app": "valve_control", "schema": 1, "command_channel": "io_commands",
  "valves": [{"target": "flood_gate_1", "name": "Flood Gate 1", "mode": "timed",
              "default_open_s": 1800, "max_open_s": 14400}],
  "updated_ts": 1795050000000}}
```

The edge controller should use these to enforce the mode and maximum duration locally,
including for opens that don't come from this app.

### Tags

Per valve, prefixed with its key (its target id made lower-case, e.g. `flood_gate_1_`):

| Tag | Description |
|-----|-------------|
| `<key>_state` | `open` / `closed`; `null` before the first reading |
| `<key>_last_changed` | When the reported state last changed (ms since epoch) |
| `<key>_pending` | `open` / `close` while a command awaits confirmation, else `null` |
| `<key>_open_until` | When a confirmed timed open is due to close (ms since epoch) |
| `<key>_position` | Reported position (%), when configured |
| `<key>_last_command` | The last command and its outcome |

And `open_count`: how many valves report open (`null` before any reading).

### Notifications

| Notification | When |
|--------------|------|
| **Command not confirmed** | A command's valve did not report the commanded state within the confirmation timeout |
| **Open too long** | A valve has reported open for longer than its Max Open Duration |

Each fires once per occurrence, and never from missing data.

<br/>

### Need Help?

- Email: support@doover.com
- [Doover Documentation](https://docs.doover.com)

<br/>

## License

This app is licensed under the [Apache License 2.0](https://github.com/getdoover/valve-control/blob/main/LICENSE).
