"""The app against faked tags and API: readings in, commands and settings out."""

from types import SimpleNamespace

from conftest import APP_KEY, FakeApi, FakeTagManager, make_config

from valve_control import application
from valve_control.app_notifications import ValveControlNotifications
from valve_control.app_tags import ValveControlTags
from valve_control.app_ui import ValveControlUI
from valve_control.application import ValveControlApplication

T0 = 1_795_050_000_000

TURKEYS_NEST = [
    {
        "name": "Valve 1",
        "target": "valve_1",
        "state_path": "$io_state.valve_1",
        "mode": "Timed",
        "default_open_min": 60.0,
        "max_open_min": 720.0,
    },
    {
        "name": "Valve 3",
        "target": "valve_3",
        "state_path": "$io_state.valve_3",
        "mode": "Timed",
        "default_open_min": 60.0,
        "max_open_min": 720.0,
    },
]


async def make_app(monkeypatch, now=T0, aggregates=None, **config):
    monkeypatch.setattr(application, "now_ms", lambda: now)
    app = ValveControlApplication()
    app.config = make_config(**config)
    app.app_key = APP_KEY
    app.api = FakeApi(aggregates)
    app.tag_manager = FakeTagManager()
    app.tags = ValveControlTags(APP_KEY, app.tag_manager, app.config)
    await app.tags.setup()
    app.ui = ValveControlUI(app.config, app.tags, APP_KEY)
    await app.ui.setup()
    app.notifications = ValveControlNotifications(APP_KEY, app)
    await app.setup()
    return app


async def again(app, monkeypatch, now):
    """The next invocation: a fresh instance over the same tags and API."""
    nxt = await make_app(monkeypatch, now=now)
    nxt.config, nxt.api, nxt.tag_manager = app.config, app.api, app.tag_manager
    nxt.tags = ValveControlTags(APP_KEY, app.tag_manager, app.config)
    await nxt.tags.setup()
    nxt.notifications = ValveControlNotifications(APP_KEY, nxt)
    await nxt.setup()
    return nxt


def message(channel, data):
    return SimpleNamespace(
        channel=SimpleNamespace(name=channel), message=SimpleNamespace(data=data)
    )


class Ctx:
    def __init__(self, method):
        self.method = method
        self.values = []

    async def set_value(self, value):
        self.values.append(value)


def tags(app):
    return app.tag_manager.values


def notifications(app):
    return [d for c, d in app.api.messages if c == "notifications"]


async def test_no_data_renders_and_stays_quiet(monkeypatch):
    app = await make_app(monkeypatch)
    await app.on_schedule(None)
    await app.on_message_create(message("io_state", {"something_else": 1}))
    assert tags(app).get("flood_gate_1_state") is None
    assert tags(app).get("flood_gate_1_state_text", "Unknown") == "Unknown"
    assert tags(app).get("flood_gate_1_warning_hidden", True) is True
    assert notifications(app) == []
    schema = app.ui.to_schema()
    panel = schema["children"]["flood_gate_1__panel"]
    assert set(panel["children"]) >= {
        "flood_gate_1__state",
        "flood_gate_1__open_for",
        "flood_gate_1__close",
    }


async def test_deployment_seeds_settings_when_the_channel_is_empty(monkeypatch):
    app = await make_app(monkeypatch, valves=TURKEYS_NEST)
    await app.on_deployment(None)
    ((channel, data, kw),) = app.api.aggregate_writes
    assert channel == "control_settings" and kw == {"replace_data": True}
    payload = data[APP_KEY]
    assert payload["app"] == "valve_control" and payload["schema"] == 1
    assert [v["target"] for v in payload["valves"]] == ["valve_1", "valve_3"]
    assert (
        payload["valves"][0]["default_open_s"] == 3600
        and payload["valves"][0]["max_open_s"] == 43200
    )


async def test_deployment_merges_into_other_apps_settings(monkeypatch):
    app = await make_app(
        monkeypatch, aggregates={"control_settings": {"pump_control_1": {"x": 1}}}
    )
    await app.on_deployment(None)
    ((channel, data, kw),) = app.api.aggregate_writes
    assert list(data) == [APP_KEY] and kw == {"replace_keys": [APP_KEY]}


async def test_open_for_sends_a_command_and_waits_for_the_reading(monkeypatch):
    app = await make_app(monkeypatch)
    ctx = Ctx("flood_gate_1__open_for")
    await app.on_control(ctx, 45)
    assert ctx.values == [45]
    ((channel, cmd),) = app.api.messages
    assert channel == "io_commands"
    assert cmd["command"] == "open" and cmd["target"] == "flood_gate_1"
    assert (
        cmd["duration_s"] == 2700
        and cmd["app_key"] == APP_KEY
        and cmd["app"] == "valve_control"
    )
    assert cmd["ts"] == T0 and cmd["params"] == {} and cmd["request_id"]
    assert tags(app)["flood_gate_1_pending"] == "open"
    assert tags(app)["flood_gate_1_warning_hidden"] is False

    nxt = await again(app, monkeypatch, T0 + 15_000)
    await nxt.on_message_create(
        message("io_state", {"flood_gate_1": True, "valve_9": 0})
    )
    t = tags(nxt)
    assert t["flood_gate_1_state"] == "open" and t["flood_gate_1_state_text"] == "Open"
    assert (
        t["flood_gate_1_pending"] is None and t["flood_gate_1_warning_hidden"] is True
    )
    assert (
        t["flood_gate_1_open_until"] == T0 + 2_700_000
        and t["flood_gate_1_open_until_hidden"] is False
    )
    assert t["flood_gate_1_last_changed"] == T0 + 15_000
    assert t["open_count"] == 1


async def test_too_long_an_open_is_not_sent(monkeypatch):
    app = await make_app(monkeypatch)
    await app.on_control(Ctx("flood_gate_1__open_for"), 999)
    assert app.api.messages == []
    assert "longer than the 240 min maximum" in tags(app)["flood_gate_1_warning"]


async def test_close_and_latched_open(monkeypatch):
    latched = [{**TURKEYS_NEST[0], "mode": "Latched"}]
    app = await make_app(monkeypatch, valves=latched)
    assert "valve_1__open" in app.ui.get_interactions()
    await app.on_control(Ctx("valve_1__open"), None)
    await app.on_control(Ctx("valve_1__close"), None)
    (_, opened), (_, closed) = app.api.messages
    assert opened["command"] == "open" and opened["duration_s"] is None
    assert closed["command"] == "close" and closed["duration_s"] is None


async def test_unconfirmed_command_warns_and_notifies_once(monkeypatch):
    app = await make_app(monkeypatch)
    await app.on_control(Ctx("flood_gate_1__close"), None)
    later = await again(app, monkeypatch, T0 + 301_000)
    await later.on_schedule(None)
    (note,) = notifications(later)
    assert "not confirmed" in note["message"]
    assert "not confirmed" in tags(later)["flood_gate_1_warning"]
    again_later = await again(app, monkeypatch, T0 + 900_000)
    await again_later.on_schedule(None)
    assert len(notifications(again_later)) == 1


async def test_controller_rejection_is_shown(monkeypatch):
    app = await make_app(monkeypatch)
    await app.on_control(Ctx("flood_gate_1__close"), None)
    rid = app.api.messages[0][1]["request_id"]
    nxt = await again(app, monkeypatch, T0 + 2_000)
    await nxt.on_message_create(
        message("io_commands", {"request_id": rid, "ok": False, "error": "manual mode"})
    )
    assert "manual mode" in tags(nxt)["flood_gate_1_warning"]
    assert tags(nxt)["flood_gate_1_pending"] is None
    # Our own command messages on that channel are ignored.
    await nxt.on_message_create(message("io_commands", app.api.messages[0][1]))


async def test_open_too_long_notifies(monkeypatch):
    app = await make_app(monkeypatch)
    await app.on_message_create(message("io_state", {"flood_gate_1": 1}))
    nxt = await again(app, monkeypatch, T0 + 241 * 60_000)
    await nxt.on_schedule(None)
    (note,) = notifications(nxt)
    assert "longer than its 4 h maximum" in note["message"]


def shown(app, key="flood_gate_1"):
    t = tags(app)
    return (
        not t.get(f"{key}_open_hidden", False),
        not t.get(f"{key}_close_hidden", False),
    )


async def test_controls_follow_the_reported_state(monkeypatch):
    app = await make_app(monkeypatch)
    await app.on_schedule(None)
    assert shown(app) == (True, True)  # no data: both

    panel = app.ui.to_schema()["children"]["flood_gate_1__panel"]["children"]
    assert "open_hidden" in str(panel["flood_gate_1__open_for"]["hidden"])
    assert "close_hidden" in str(panel["flood_gate_1__close"]["hidden"])

    await app.on_message_create(message("io_state", {"flood_gate_1": 0}))
    assert shown(app) == (True, False)  # closed: only Open

    await app.on_control(Ctx("flood_gate_1__open_for"), 30)
    assert shown(app) == (True, True)  # pending: both

    nxt = await again(app, monkeypatch, T0 + 10_000)
    await nxt.on_message_create(message("io_state", {"flood_gate_1": 1}))
    assert shown(nxt) == (False, True)  # open: only Close

    await nxt.on_control(Ctx("flood_gate_1__close"), True)
    late = await again(nxt, monkeypatch, T0 + 10_000 + 301_000)
    await late.on_schedule(None)
    assert shown(late) == (True, True)  # not confirmed: both
