import pytest

from valve_control import model
from valve_control.model import CLOSED, OPEN, Labels, Status, build_valve

LABELS = Labels()
T0 = 1_795_050_000_000


def gate(**raw):
    base = {
        "name": "Flood Gate 1",
        "target": "flood_gate_1",
        "state_path": "$io_state.flood_gate_1",
        "mode": "Timed",
        "default_open_min": 30.0,
        "max_open_min": 240.0,
    }
    base.update(raw)
    return build_valve(0, base, set())


# ------------------------------------------------------------------ inputs


def test_message_paths():
    assert model.parse_message_path("$io_state.flood_gate_1") == (
        "io_state",
        ["flood_gate_1"],
    )
    assert model.parse_message_path("$io_state:a.b") == ("io_state", ["a", "b"])
    assert model.parse_message_path("") is None
    assert model.parse_message_path(None) is None
    for bad in ("io_state.x", "$io_state", "$.x"):
        with pytest.raises(ValueError):
            model.parse_message_path(bad)
    assert model.extract_path_value({"a": {"b": [5, 6]}}, ["a", "b", "1"]) == 6
    assert model.extract_path_value({"a": 1}, ["b"]) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        (0.0, False),
        (2.5, True),
        ("open", True),
        ("ON", True),
        ("closed", False),
        ("off", False),
        ("0", False),
        ("1", True),
        ("", False),
        (None, None),
        ("banana", None),
        ({"x": 1}, None),
    ],
)
def test_truthy_is_open(raw, expected):
    assert model.reading_is_open(raw) is expected


def test_valve_keys_are_safe_and_unique():
    taken = set()
    assert model.valve_key("Flood-Gate 1", 0, taken) == "flood_gate_1"
    assert model.valve_key("flood_gate_1", 1, taken) == "flood_gate_1_2"
    assert model.valve_key("", 2, taken) == "valve_3"
    assert model.valve_key("a__b", 3, taken) == "a_b"


def test_build_valve_flags_config_problems():
    v = build_valve(0, {"name": "", "target": "", "state_path": "nope"}, set())
    assert v.name == "Valve 1" and v.key == "valve_1"
    assert any("target" in e for e in v.config_errors)
    assert any("state input" in e for e in v.config_errors)
    assert gate().config_errors == []
    # A default longer than the max is held to the max.
    assert gate(default_open_min=500.0).default_open_min == 240.0


# ------------------------------------------------------------------ readings and commands


def test_no_data_means_unknown_and_quiet():
    v, s = gate(), Status()
    assert LABELS.state(s.state) == "Unknown"
    check = model.check_timers(s, v, T0 + 10**9, 300, LABELS)
    assert check.command_timed_out is None and check.open_too_long is None
    assert model.warning_text(s, v, T0, LABELS) is None


def test_timed_open_is_pending_until_reported_then_counts_down():
    v, s = gate(), Status(state=CLOSED, last_changed=T0 - 1000)
    duration = model.open_duration_s(v, 45)
    assert duration == 2700
    rid = model.begin_command(s, v, OPEN, duration, T0, LABELS, request_id="r1")
    assert rid == "r1" and s.pending == OPEN
    assert "waiting for Flood Gate 1 to report Open" in model.warning_text(
        s, v, T0, LABELS
    )

    # A reading that still says closed changes nothing.
    model.apply_reading(s, False, None, T0 + 5000, LABELS)
    assert s.pending == OPEN

    model.apply_reading(s, True, None, T0 + 20_000, LABELS)
    assert s.state == OPEN and s.pending is None and s.last_changed == T0 + 20_000
    assert s.open_until == T0 + 2700 * 1000
    assert s.last_command == "Open for 45 min: confirmed"
    assert model.warning_text(s, v, T0 + 20_000, LABELS) is None

    model.apply_reading(s, False, None, T0 + 60_000, LABELS)
    assert s.state == CLOSED and s.open_until is None


def test_unconfirmed_command_times_out_once():
    v, s = gate(), Status(state=CLOSED)
    model.begin_command(s, v, OPEN, 1800, T0, LABELS)
    assert model.check_timers(s, v, T0 + 299_000, 300, LABELS).command_timed_out is None
    check = model.check_timers(s, v, T0 + 301_000, 300, LABELS)
    assert (
        "not confirmed" in check.command_timed_out
        and "still reports Closed" in check.command_timed_out
    )
    assert s.pending is None and s.problem
    assert model.check_timers(s, v, T0 + 900_000, 300, LABELS).command_timed_out is None
    # The valve then moving clears the problem.
    model.apply_reading(s, True, None, T0 + 950_000, LABELS)
    assert s.problem is None


def test_reply_rejected_and_reply_confirming_unchanged_state():
    v, s = gate(), Status(state=CLOSED)
    model.begin_command(s, v, OPEN, 1800, T0, LABELS, request_id="r1")
    assert not model.apply_reply(s, {"request_id": "other", "ok": False}, LABELS)
    assert model.apply_reply(
        s, {"request_id": "r1", "ok": False, "error": "interlocked"}, LABELS
    )
    assert s.pending is None and "interlocked" in s.problem

    s = Status(state=CLOSED)
    model.begin_command(s, v, "close", None, T0, LABELS, request_id="r2")
    assert model.apply_reply(s, {"request_id": "r2", "ok": True}, LABELS)
    assert s.pending is None and s.last_command == "Closed: confirmed"

    s = Status(state=CLOSED)
    model.begin_command(s, v, OPEN, 600, T0, LABELS, request_id="r3")
    assert model.apply_reply(s, {"request_id": "r3", "ok": True, "error": None}, LABELS)
    assert s.pending == OPEN and "accepted" in s.last_command


def test_open_too_long_notifies_once_per_opening():
    v, s = gate(max_open_min=60.0), Status()
    model.apply_reading(s, True, None, T0, LABELS)  # opened locally
    assert s.open_until is None
    assert model.check_timers(s, v, T0 + 3_500_000, 0, LABELS).open_too_long is None
    text = model.check_timers(s, v, T0 + 3_700_000, 0, LABELS).open_too_long
    assert (
        text == "Flood Gate 1 has been Open for 62 min, longer than its 60 min maximum"
    )
    assert model.check_timers(s, v, T0 + 4_000_000, 0, LABELS).open_too_long is None
    assert "longer than" in model.warning_text(s, v, T0 + 4_000_000, LABELS)
    model.apply_reading(s, False, None, T0 + 4_100_000, LABELS)
    model.apply_reading(s, True, None, T0 + 4_200_000, LABELS)
    assert not s.overrun_notified


def test_no_max_means_no_overrun():
    v, s = gate(max_open_min=0.0), Status()
    model.apply_reading(s, True, None, T0, LABELS)
    assert model.check_timers(s, v, T0 + 10**10, 0, LABELS).open_too_long is None


def test_open_durations():
    v = gate()
    assert model.open_duration_s(v, None) == 1800
    assert model.open_duration_s(v, "15") == 900
    for bad in (0, -1, 241, "abc"):
        with pytest.raises(ValueError):
            model.open_duration_s(v, bad)
    assert model.open_duration_s(gate(mode="Latched"), 99999) is None


def test_custom_labels():
    labels = Labels(open="Raised", closed="Lowered")
    v, s = gate(), Status(state=CLOSED)
    model.begin_command(s, v, OPEN, 600, T0, labels)
    assert s.last_command == "Raised for 10 min: sent"
    assert labels.state(OPEN) == "Raised"


# ------------------------------------------------------------------ wire formats


def test_command_message_shape():
    v = gate()
    msg = model.command_message(OPEN, v, 1800, "rid", "valve_control_1", T0)
    assert msg == {
        "command": "open",
        "target": "flood_gate_1",
        "duration_s": 1800,
        "params": {},
        "request_id": "rid",
        "app_key": "valve_control_1",
        "app": "valve_control",
        "ts": T0,
    }
    assert model.command_message("close", v, 1800, "rid", "k", T0)["duration_s"] is None


def test_settings_payload_shape():
    valves = [
        gate(),
        build_valve(
            1,
            {
                "name": "Valve 3",
                "target": "valve_3",
                "mode": "Latched",
                "state_path": "$io_state.valve_3",
                "max_open_min": 720.0,
            },
            set(),
        ),
    ]
    payload = model.settings_payload(valves, "io_commands", T0)
    assert payload == {
        "app": "valve_control",
        "schema": 1,
        "command_channel": "io_commands",
        "valves": [
            {
                "target": "flood_gate_1",
                "name": "Flood Gate 1",
                "mode": "timed",
                "default_open_s": 1800,
                "max_open_s": 14400,
            },
            {
                "target": "valve_3",
                "name": "Valve 3",
                "mode": "latched",
                "default_open_s": None,
                "max_open_s": 43200,
            },
        ],
        "updated_ts": T0,
    }
