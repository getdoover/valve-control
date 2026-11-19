"""Valve Control: a remote control for on/off valves and gates.

Readings arrive as messages on subscribed channels and are read at each valve's
configured message path. The page's controls arrive on ui_cmds and go out as commands on
the Command Channel. The valves' modes and durations are written to the Settings Channel
for the edge controller to enforce. A schedule checks for unconfirmed commands and
over-long opens between readings.
"""

import logging
import re
import time

from pydoover import ui
from pydoover.models import (
    DeploymentEvent,
    MessageCreateEvent,
    NotFoundError,
    ScheduleEvent,
)
from pydoover.processor import Application

from . import model
from .app_config import ValveControlConfig
from .app_notifications import ValveControlNotifications
from .app_tags import VALVE_TAGS, ValveControlTags, tag_name
from .app_ui import CLOSE, OPEN, OPEN_FOR, ValveControlUI

log = logging.getLogger(__name__)

CONTROL_RE = re.compile(r"^(?P<key>[a-z0-9_]+?)__(?P<action>open_for|open|close)$")


def now_ms() -> int:
    return int(time.time() * 1000)


class ValveControlApplication(Application):
    config_cls = ValveControlConfig
    tags_cls = ValveControlTags
    ui_cls = ValveControlUI
    notifications_cls = ValveControlNotifications

    config: ValveControlConfig
    tags: ValveControlTags
    ui: ValveControlUI
    notifications: ValveControlNotifications

    async def setup(self):
        self.valves = self.config.valve_list() if self.config is not None else []
        self.labels = model.Labels(
            open=self._str("open_label", "Open"),
            closed=self._str("closed_label", "Closed"),
        )
        self.now = now_ms()

    def _str(self, attr: str, default: str) -> str:
        try:
            return getattr(self.config, attr).value or default
        except (AttributeError, ValueError):
            return default

    def _number(self, attr: str, default: float) -> float:
        try:
            value = getattr(self.config, attr).value
        except (AttributeError, ValueError):
            return default
        return default if value is None else float(value)

    @property
    def command_channel(self) -> str:
        return self._str("command_channel", "io_commands")

    @property
    def settings_channel(self) -> str:
        return self._str("settings_channel", "control_settings")

    # ------------------------------------------------------------------ status <-> tags

    def _load(self, valve: model.Valve) -> model.Status:
        status = model.Status()
        for field in model.Status.__dataclass_fields__:
            tag = self.tags.get(tag_name(valve.key, field))
            if tag is not None and tag.value is not None:
                setattr(status, field, tag.value)
        for field in (
            "last_changed",
            "pending_since",
            "open_until",
            "pending_duration_s",
        ):
            value = getattr(status, field)
            if value is not None:
                setattr(status, field, int(value))
        return status

    async def _set(self, name: str, value):
        tag = self.tags.get(name)
        if tag is not None and tag.value != value:
            await tag.set(value)

    async def _save(self, valve: model.Valve, status: model.Status):
        for field in model.Status.__dataclass_fields__:
            if field in VALVE_TAGS:
                await self._set(tag_name(valve.key, field), getattr(status, field))
        await self._set(
            tag_name(valve.key, "state_text"), self.labels.state(status.state)
        )
        warning = model.warning_text(status, valve, self.now, self.labels)
        await self._set(tag_name(valve.key, "warning"), warning or "")
        await self._set(tag_name(valve.key, "warning_hidden"), warning is None)
        await self._set(
            tag_name(valve.key, "open_until_hidden"),
            not (status.state == model.OPEN and status.open_until is not None),
        )
        shown = model.controls(status)
        await self._set(tag_name(valve.key, "open_hidden"), not shown.show_open)
        await self._set(tag_name(valve.key, "close_hidden"), not shown.show_close)

    async def _check_and_save(self, valve: model.Valve, status: model.Status):
        timeout_s = self._number("confirm_timeout_s", 300.0)
        check = model.check_timers(status, valve, self.now, timeout_s, self.labels)
        await self._save(valve, status)
        if check.command_timed_out:
            log.warning("%s", check.command_timed_out)
            await self.notifications.command_not_confirmed.send(check.command_timed_out)
        if check.open_too_long:
            log.warning("%s", check.open_too_long)
            await self.notifications.open_too_long.send(check.open_too_long)

    async def _check_all(self, statuses: dict[str, model.Status] | None = None):
        statuses = statuses or {}
        open_count, known = 0, 0
        for valve in self.valves:
            status = statuses.get(valve.key) or self._load(valve)
            await self._check_and_save(valve, status)
            if status.state is not None:
                known += 1
                open_count += status.state == model.OPEN
        await self._set("open_count", open_count if known else None)

    # ------------------------------------------------------------------ events

    async def on_deployment(self, event: DeploymentEvent):
        await self._publish_settings()
        await self._check_all()

    async def on_schedule(self, event: ScheduleEvent):
        await self._check_all()

    async def on_message_create(self, event: MessageCreateEvent):
        channel = event.channel.name
        data = event.message.data or {}
        statuses: dict[str, model.Status] = {}

        if channel == self.command_channel and model.is_reply(data):
            for valve in self.valves:
                status = self._load(valve)
                if model.apply_reply(status, data, self.labels):
                    log.info("%s: controller answered %s", valve.name, data)
                    statuses[valve.key] = status

        for valve in self.valves:
            is_open, position = self._read(valve, channel, data)
            if is_open is None and position is None:
                continue
            status = statuses.get(valve.key) or self._load(valve)
            model.apply_reading(status, is_open, position, self.now, self.labels)
            statuses[valve.key] = status

        # ui_cmds and other channels' messages still give the timers a look.
        await self._check_all(statuses)

    def _read(
        self, valve: model.Valve, channel: str, data
    ) -> tuple[bool | None, float | None]:
        is_open = position = None
        if valve.state_path and valve.state_path[0] == channel:
            raw = model.extract_path_value(data, valve.state_path[1])
            is_open = model.reading_is_open(raw)
            if raw is not None and is_open is None:
                log.warning(
                    "%s: state %r is neither open nor closed; ignored", valve.name, raw
                )
        if valve.position_path and valve.position_path[0] == channel:
            raw = model.extract_path_value(data, valve.position_path[1])
            position = model.reading_number(raw)
            if raw is not None and position is None:
                log.warning("%s: position %r is not a number; ignored", valve.name, raw)
            elif position is not None and valve.state_path is None:
                is_open = position > 0
        return is_open, position

    # ------------------------------------------------------------------ settings

    async def _publish_settings(self):
        payload = model.settings_payload(self.valves, self.command_channel, self.now)
        channel = self.settings_channel
        try:
            existing = await self.api.fetch_channel_aggregate(channel)
            data = getattr(existing, "data", None)
        except NotFoundError:
            data = None
        if isinstance(data, dict) and data:
            # Merge: other apps keep their keys; ours is replaced whole so removed
            # valves go with it.
            await self.api.update_channel_aggregate(
                channel, {self.app_key: payload}, replace_keys=[self.app_key]
            )
        else:
            # A missing or null aggregate takes no PATCH, so seed it whole.
            await self.api.update_channel_aggregate(
                channel, {self.app_key: payload}, replace_data=True
            )
        log.info("settings written to %s: %s", channel, payload)

    # ------------------------------------------------------------------ controls

    @ui.handler(CONTROL_RE, auto_update=False)
    async def on_control(self, ctx, value):
        match = CONTROL_RE.match(ctx.method)
        valve = (
            next((v for v in self.valves if v.key == match["key"]), None)
            if match
            else None
        )
        if valve is None:
            log.warning("control %s matches no configured valve", ctx.method)
            return
        action = match["action"]
        if action == OPEN_FOR:
            # Keep the operator's duration in the box for next time.
            await ctx.set_value(value)
        else:
            await ctx.set_value(None)
        verb = model.OPEN if action in (OPEN, OPEN_FOR) else CLOSE
        await self._command(valve, verb, value if action == OPEN_FOR else None)

    async def _command(self, valve: model.Valve, verb: str, minutes=None):
        status = self._load(valve)
        if not valve.target:
            status.problem = "Not sent: this valve has no target id configured"
            await self._check_and_save(valve, status)
            return
        try:
            duration_s = (
                model.open_duration_s(valve, minutes) if verb == model.OPEN else None
            )
        except ValueError as e:
            status.problem = f"Not sent: {e}"
            status.last_command = f"{self.labels.open}: not sent ({e})"
            await self._check_and_save(valve, status)
            return

        request_id = model.begin_command(
            status, valve, verb, duration_s, self.now, self.labels
        )
        message = model.command_message(
            verb, valve, duration_s, request_id, self.app_key, self.now
        )
        await self.api.create_message(self.command_channel, message)
        log.info("sent %s", message)
        await self._check_and_save(valve, status)
