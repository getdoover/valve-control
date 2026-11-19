"""Fakes for the Doover runtime: tags in a dict, API calls recorded."""

import pytest

from valve_control.app_config import ValveControlConfig

APP_KEY = "valve_control_abc123"


class FakeTagManager:
    def __init__(self):
        self.values: dict[str, object] = {}

    def get_tag(self, key, default=None, app_key=None):
        return self.values.get(key, default)

    async def set_tag(self, key, value, app_key=None, log=False):
        self.values[key] = value

    async def commit_tags(self):
        pass


class FakeApi:
    def __init__(self, aggregates=None):
        self.messages: list[tuple[str, dict]] = []
        self.aggregate_writes: list[tuple[str, dict, dict]] = []
        self.aggregates = aggregates or {}

    async def create_message(self, channel, data, **kw):
        self.messages.append((channel, data))

    async def update_channel_aggregate(self, channel, data, **kw):
        self.aggregate_writes.append((channel, data, kw))

    async def fetch_channel_aggregate(self, channel, **kw):
        from types import SimpleNamespace

        from pydoover.models import NotFoundError

        if channel not in self.aggregates:
            raise NotFoundError(404, "not found")
        return SimpleNamespace(data=self.aggregates[channel])


def make_config(**overrides):
    data = {
        "valves": [
            {
                "name": "Flood Gate 1",
                "target": "flood_gate_1",
                "state_path": "$io_state.flood_gate_1",
                "mode": "Timed",
                "default_open_min": 30.0,
                "max_open_min": 240.0,
            }
        ],
        "dv_proc_subscriptions": ["io_state", "ui_cmds", "io_commands"],
        "dv_proc_schedules": "rate(5 minutes)",
    }
    data.update(overrides)
    cfg = ValveControlConfig()
    cfg._inject_deployment_config(data)
    return cfg


@pytest.fixture
def config():
    return make_config()
