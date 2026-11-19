"""Valve Control's logic, free of the Doover runtime so it can be tested directly.

A valve here is anything with two states driven by one output: a solenoid valve, an
actuated ball valve, a sluice or flood gate. The edge controller is the source of truth:
it executes the commands this app sends and reports what the valve is actually doing. So
the state shown is always the reported one, and a command is only "pending" until the
reported state agrees with it (or a timeout passes).
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

APP_NAME = "valve_control"
SETTINGS_SCHEMA = 1

OPEN = "open"
CLOSED = "closed"

TIMED = "Timed"
LATCHED = "Latched"

# Reported string values that mean open / closed. Anything else is not understood
# and is treated as no reading rather than guessed at.
_OPEN_WORDS = {"open", "opened", "opening", "on", "true", "yes", "1", "running"}
_CLOSED_WORDS = {
    "closed",
    "close",
    "closing",
    "shut",
    "off",
    "false",
    "no",
    "0",
    "stopped",
    "",
}

_KEY_RE = re.compile(r"[^a-z0-9]+")


# --------------------------------------------------------------------------- paths


def parse_message_path(value: Any) -> tuple[str, list[str]] | None:
    """Split '$channel.path.to.value' (or '$channel:path') into (channel, parts).

    Returns None for an empty path (an optional input that isn't configured) and
    raises ValueError for one that is set but malformed.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("A message path must be a string like '$channel.key'.")
    path = value.strip()
    if not path:
        return None
    if not path.startswith("$"):
        raise ValueError(f"Message path {path!r} must start with '$channel'.")

    body = path[1:]
    if ":" in body:
        channel, raw_path = body.split(":", 1)
    elif "." in body:
        channel, raw_path = body.split(".", 1)
    else:
        raise ValueError(f"Message path {path!r} needs a value path after the channel.")

    channel = channel.strip()
    parts = [part.strip() for part in raw_path.split(".") if part.strip()]
    if not channel or not parts:
        raise ValueError(
            f"Message path {path!r} needs both a channel and a value path."
        )
    return channel, parts


def extract_path_value(data: Any, path: list[str]) -> Any:
    """The value at `path` in a message's data, or None when it isn't there."""
    value = data
    for part in path:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list):
            try:
                value = value[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if value is None:
            return None
    return value


def reading_is_open(value: Any) -> bool | None:
    """Interpret a raw reported state: truthy is open. None when not understood."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return value != 0
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _OPEN_WORDS:
            return True
        if word in _CLOSED_WORDS:
            return False
        try:
            return float(word) != 0
        except ValueError:
            return None
    return None


def reading_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


# --------------------------------------------------------------------------- config


def valve_key(target: str, index: int, taken: set[str]) -> str:
    """A stable identifier for a valve's tags and UI elements, from its target id.

    Lower-case letters, digits and single underscores, so the UI's element names can
    join it to an action with a double underscore.
    """
    key = _KEY_RE.sub("_", (target or "").lower()).strip("_")
    if not key:
        key = f"valve_{index + 1}"
    base, n = key, 2
    while key in taken:
        key = f"{base}_{n}"
        n += 1
    taken.add(key)
    return key


@dataclass
class Valve:
    key: str
    name: str
    target: str
    mode: str = TIMED
    default_open_min: float = 30.0
    max_open_min: float = 0.0
    state_path: tuple[str, list[str]] | None = None
    position_path: tuple[str, list[str]] | None = None
    config_errors: list[str] = field(default_factory=list)

    @property
    def timed(self) -> bool:
        return self.mode == TIMED

    @property
    def default_open_s(self) -> int | None:
        return (
            round(self.default_open_min * 60)
            if self.default_open_min > 0
            else None
        )

    @property
    def max_open_s(self) -> int | None:
        return round(self.max_open_min * 60) if self.max_open_min > 0 else None


def _number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(number) else number


def build_valve(index: int, raw: dict[str, Any], taken: set[str]) -> Valve:
    """One valve from its config values (a plain dict, so tests needn't build a Schema)."""
    target = str(raw.get("target") or "").strip()
    name = str(raw.get("name") or "").strip() or f"Valve {index + 1}"
    mode = raw.get("mode") if raw.get("mode") in (TIMED, LATCHED) else TIMED
    valve = Valve(
        key=valve_key(target, index, taken),
        name=name,
        target=target,
        mode=mode,
        default_open_min=max(_number(raw.get("default_open_min"), 30.0), 0.0),
        max_open_min=max(_number(raw.get("max_open_min"), 0.0), 0.0),
    )
    if not target:
        valve.config_errors.append("no target id is set, so commands can't be sent")
    for attr, label in (
        ("state_path", "state input"),
        ("position_path", "position input"),
    ):
        try:
            setattr(valve, attr, parse_message_path(raw.get(attr)))
        except ValueError as e:
            valve.config_errors.append(f"{label}: {e}")
    if valve.state_path is None and valve.position_path is None:
        valve.config_errors.append("no state or position input is set")
    if (
        valve.timed
        and valve.max_open_s
        and valve.default_open_s
        and valve.default_open_s > valve.max_open_s
    ):
        valve.default_open_min = valve.max_open_min
    return valve


# --------------------------------------------------------------------------- status


@dataclass
class Status:
    """A valve's runtime state, persisted as its tags between invocations."""

    state: str | None = None  # OPEN / CLOSED, None before the first reading
    position: float | None = None
    last_changed: int | None = None  # ms
    pending: str | None = None  # the verb awaiting confirmation
    pending_since: int | None = None  # ms
    pending_request_id: str | None = None
    pending_duration_s: int | None = None
    open_until: int | None = None  # ms; a timed open's expected auto-close
    problem: str | None = None  # a timed-out or rejected command, shown until resolved
    last_command: str | None = None
    timeout_notified: bool = False
    overrun_notified: bool = False


@dataclass
class Labels:
    open: str = "Open"
    closed: str = "Closed"
    unknown: str = "Unknown"

    def state(self, state: str | None) -> str:
        if state == OPEN:
            return self.open
        if state == CLOSED:
            return self.closed
        return self.unknown

    def verb(self, verb: str | None) -> str:
        return (
            self.open
            if verb == OPEN
            else self.closed
            if verb == "close"
            else (verb or "")
        )


def target_state(verb: str) -> str:
    return OPEN if verb == OPEN else CLOSED


def fmt_minutes(seconds: float) -> str:
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} min" if minutes >= 1 else f"{seconds:.0f} s"
    hours, rem = divmod(round(minutes), 60)
    return f"{hours} h {rem} min" if rem else f"{hours} h"


def describe_command(verb: str, duration_s: int | None, labels: Labels) -> str:
    if verb == OPEN and duration_s:
        return f"{labels.open} for {fmt_minutes(duration_s)}"
    return labels.verb(verb)


def apply_reading(
    status: Status,
    is_open: bool | None,
    position: float | None,
    now_ms: int,
    labels: Labels,
) -> None:
    """Fold one reported reading into the status."""
    if position is not None:
        status.position = position
    if is_open is None:
        return

    new_state = OPEN if is_open else CLOSED
    if new_state != status.state:
        status.last_changed = now_ms
        status.overrun_notified = False
        if new_state == CLOSED:
            status.open_until = None
        elif status.pending != OPEN:
            # Opened by something other than our command (a local HMI, a schedule
            # on the controller): how long it stays open isn't known here.
            status.open_until = None
    status.state = new_state

    if status.pending and new_state == target_state(status.pending):
        _confirm(status, labels)
    elif status.problem and not status.pending:
        # The valve has moved since the failed command; the old problem no longer
        # describes it.
        if status.last_changed == now_ms:
            status.problem = None


def _confirm(status: Status, labels: Labels) -> None:
    verb, duration_s = status.pending, status.pending_duration_s
    if verb == OPEN and duration_s:
        status.open_until = (status.pending_since or 0) + duration_s * 1000
    status.last_command = f"{describe_command(verb, duration_s, labels)}: confirmed"
    status.pending = None
    status.pending_since = None
    status.pending_request_id = None
    status.pending_duration_s = None
    status.problem = None
    status.timeout_notified = False


def begin_command(
    status: Status,
    valve: Valve,
    verb: str,
    duration_s: int | None,
    now_ms: int,
    labels: Labels,
    request_id: str | None = None,
) -> str:
    """Record a command as sent and pending. Returns its request id."""
    request_id = request_id or str(uuid.uuid4())
    status.pending = verb
    status.pending_since = now_ms
    status.pending_request_id = request_id
    status.pending_duration_s = duration_s if verb == OPEN else None
    status.problem = None
    status.timeout_notified = False
    status.last_command = f"{describe_command(verb, duration_s, labels)}: sent"
    return request_id


def apply_reply(status: Status, reply: dict[str, Any], labels: Labels) -> bool:
    """Fold the controller's optional {request_id, ok, error} answer in.

    Returns True when it answered this valve's pending command.
    """
    if not status.pending or reply.get("request_id") != status.pending_request_id:
        return False
    what = describe_command(status.pending, status.pending_duration_s, labels)
    if reply.get("ok") is False or reply.get("error"):
        error = str(reply.get("error") or "no reason given")
        status.problem = f"{what} was rejected by the controller: {error}"
        status.last_command = f"{what}: rejected ({error})"
        status.pending = None
        status.pending_since = None
        status.pending_request_id = None
        status.pending_duration_s = None
        return True
    if status.state == target_state(status.pending):
        # Already in the commanded state (re-opening an open valve to extend it,
        # closing a closed one): the reading won't change, so the answer confirms it.
        _confirm(status, labels)
    else:
        status.last_command = f"{what}: accepted, waiting for the valve to report {labels.state(target_state(status.pending))}"
    return True


@dataclass
class Check:
    command_timed_out: str | None = None  # notification text
    open_too_long: str | None = None  # notification text


def check_timers(
    status: Status, valve: Valve, now_ms: int, timeout_s: float, labels: Labels
) -> Check:
    """Time-based transitions: an unconfirmed command, a valve open too long.

    Nothing here fires without data: a timeout needs a command to have been sent,
    and an overrun needs the valve to have reported open.
    """
    result = Check()

    if status.pending and status.pending_since is not None and timeout_s > 0:
        waited_s = (now_ms - status.pending_since) / 1000
        if waited_s >= timeout_s:
            what = describe_command(status.pending, status.pending_duration_s, labels)
            reported = labels.state(status.state)
            text = (
                f"{what} not confirmed after {fmt_minutes(waited_s)}: "
                f"{valve.name} still reports {reported}"
            )
            status.problem = text
            status.last_command = f"{what}: not confirmed"
            status.pending = None
            status.pending_since = None
            status.pending_request_id = None
            status.pending_duration_s = None
            if not status.timeout_notified:
                status.timeout_notified = True
                result.command_timed_out = text

    max_s = valve.max_open_s
    if status.state == OPEN and status.last_changed is not None and max_s:
        open_s = (now_ms - status.last_changed) / 1000
        if open_s > max_s and not status.overrun_notified:
            status.overrun_notified = True
            result.open_too_long = (
                f"{valve.name} has been {labels.open} for {fmt_minutes(open_s)}, "
                f"longer than its {fmt_minutes(max_s)} maximum"
            )

    return result


def warning_text(
    status: Status, valve: Valve, now_ms: int, labels: Labels
) -> str | None:
    """What the valve's warning indicator says, or None to hide it."""
    if status.pending:
        what = describe_command(status.pending, status.pending_duration_s, labels)
        return f"{what} sent: waiting for {valve.name} to report {labels.state(target_state(status.pending))}"
    if status.problem:
        return status.problem
    max_s = valve.max_open_s
    if status.state == OPEN and status.last_changed is not None and max_s:
        open_s = (now_ms - status.last_changed) / 1000
        if open_s > max_s:
            return (
                f"{labels.open} for {fmt_minutes(open_s)}, longer than its "
                f"{fmt_minutes(max_s)} maximum"
            )
    if valve.config_errors:
        return f"Check the configuration: {'; '.join(valve.config_errors)}"
    return None


# --------------------------------------------------------------------------- controls


@dataclass
class Controls:
    """Which of a valve's controls the page shows."""

    show_open: bool = True
    show_close: bool = True


def controls(status: Status) -> Controls:
    """Offer the control that changes the reported state, and only that one.

    Closed shows only Open; Open shows only Close. When the reported state can't be
    trusted -- no reading yet, a command still awaiting confirmation, or a command that
    timed out or was rejected -- both are shown, because then an explicit close is the
    safe thing to have to hand whatever the valve is actually doing.
    """
    if status.pending or status.problem or status.state not in (OPEN, CLOSED):
        return Controls(show_open=True, show_close=True)
    return Controls(show_open=status.state == CLOSED, show_close=status.state == OPEN)


# --------------------------------------------------------------------------- open request


def open_duration_s(valve: Valve, minutes: Any) -> int | None:
    """Seconds for a timed open of `minutes` (None: the default). Raises ValueError."""
    if not valve.timed:
        return None
    if minutes is None or minutes == "":
        minutes = valve.default_open_min
    try:
        minutes = float(minutes)
    except (TypeError, ValueError):
        raise ValueError(f"{minutes!r} is not a number of minutes") from None
    if math.isnan(minutes) or minutes <= 0:
        raise ValueError("the duration must be more than 0 minutes")
    if valve.max_open_min > 0 and minutes > valve.max_open_min:
        raise ValueError(
            f"{minutes:g} min is longer than the {valve.max_open_min:g} min maximum"
        )
    return round(minutes * 60)


# --------------------------------------------------------------------------- wire formats


def command_message(
    verb: str,
    valve: Valve,
    duration_s: int | None,
    request_id: str,
    app_key: str | None,
    now_ms: int,
) -> dict[str, Any]:
    """One command for the edge controller, on the Command Channel."""
    return {
        "command": verb,
        "target": valve.target,
        "duration_s": duration_s if verb == OPEN else None,
        "params": {},
        "request_id": request_id,
        "app_key": app_key,
        "app": APP_NAME,
        "ts": now_ms,
    }


def settings_payload(
    valves: list[Valve], command_channel: str, now_ms: int
) -> dict[str, Any]:
    """What the edge enforces locally, under this app's key on the Settings Channel."""
    return {
        "app": APP_NAME,
        "schema": SETTINGS_SCHEMA,
        "command_channel": command_channel,
        "valves": [
            {
                "target": v.target,
                "name": v.name,
                "mode": v.mode.lower(),
                "default_open_s": v.default_open_s if v.timed else None,
                "max_open_s": v.max_open_s,
            }
            for v in valves
            if v.target
        ],
        "updated_ts": now_ms,
    }


def is_reply(data: dict[str, Any]) -> bool:
    return isinstance(data, dict) and "request_id" in data and "command" not in data
