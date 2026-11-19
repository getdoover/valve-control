from pathlib import Path

from pydoover import config
from pydoover.config import ApplicationPosition
from pydoover.processor import ManySubscriptionConfig, ScheduleConfig

from .model import LATCHED, TIMED, Valve, build_valve


class ValveConfig(config.Object):
    name = config.String(
        "Name",
        name="name",
        default="Valve 1",
        description="What the valve or gate is called on the device page, e.g. 'Flood Gate 1'.",
    )
    target = config.String(
        "Target ID",
        name="target",
        default="valve_1",
        description="The output id sent in each command's `target`, as the edge controller knows it.",
    )
    state_path = config.String(
        "State Input Path",
        name="state_path",
        default="$io_state.valve_1",
        description=(
            "Message path of the valve's reported state: '$channel.path.to.value'. "
            "Truthy (true, 1, 'open', 'on') is open; falsy (false, 0, 'closed', 'off') is closed."
        ),
    )
    position_path = config.String(
        "Position Input Path",
        name="position_path",
        default="",
        description=(
            "Optional message path of the valve's position, 0-100 %. Leave empty if it "
            "only reports open/closed. With no state input, a position above 0 counts as open."
        ),
    )
    mode = config.Enum(
        "Mode",
        name="mode",
        choices=[TIMED, LATCHED],
        default=TIMED,
        description=(
            "Timed: opens for a duration, and the edge controller closes it when the time "
            "is up. Latched: stays open until it is closed."
        ),
    )
    default_open_min = config.Number(
        "Default Open Duration (min)",
        name="default_open_min",
        default=30.0,
        minimum=0.0,
        description="Timed mode: the duration offered when opening.",
    )
    max_open_min = config.Number(
        "Max Open Duration (min)",
        name="max_open_min",
        default=240.0,
        minimum=0.0,
        description=(
            "The longest a timed open may be, and how long the valve may stay open (in "
            "either mode) before a notification. 0 for no limit."
        ),
    )


class ValveControlConfig(config.Schema):
    valves = config.Array(
        "Valves",
        name="valves",
        element=ValveConfig("Valve"),
        min_items=1,
        description="The valves or gates this app controls, one panel each on the device page.",
        position=1,
    )
    open_label = config.String(
        "Open Label",
        name="open_label",
        default="Open",
        description="The word for the open state and the open command, e.g. 'Open' or 'Raised'.",
        position=2,
    )
    closed_label = config.String(
        "Closed Label",
        name="closed_label",
        default="Closed",
        description="The word for the closed state, e.g. 'Closed' or 'Lowered'.",
        position=3,
    )
    close_label = config.String(
        "Close Label",
        name="close_label",
        default="Close",
        description="The word on the close button, e.g. 'Close' or 'Lower'.",
        position=4,
    )
    confirm_timeout_s = config.Number(
        "Confirmation Timeout (s)",
        name="confirm_timeout_s",
        default=300.0,
        minimum=0.0,
        description=(
            "How long a command may wait for the valve to report the commanded state "
            "before it is flagged as not confirmed. 0 never flags it."
        ),
        position=5,
    )
    command_channel = config.String(
        "Command Channel",
        name="command_channel",
        default="io_commands",
        description="Where commands are published for the edge controller, one message per command.",
        advanced=True,
        position=6,
    )
    settings_channel = config.String(
        "Settings Channel",
        name="settings_channel",
        default="control_settings",
        description=(
            "Whose aggregate receives the valves' modes and default/max durations, under "
            "this app's key, for the edge controller to enforce locally."
        ),
        advanced=True,
        position=7,
    )
    subscription = ManySubscriptionConfig(
        default=["io_state", "ui_cmds", "io_commands"],
        required=True,
        description=(
            "Channels that invoke this app: those of the input paths, ui_cmds for the "
            "page's buttons, and the Command Channel for the controller's answers."
        ),
        advanced=True,
        position=8,
    )
    schedule = ScheduleConfig(
        default="rate(5 minutes)",
        required=True,
        description="How often unconfirmed commands and over-long opens are checked between readings.",
        advanced=True,
        position=9,
    )

    position = ApplicationPosition(position=10)

    def valve_list(self) -> list[Valve]:
        taken: set[str] = set()
        valves = []
        for index, element in enumerate(self.valves.elements):
            raw = {}
            for attr in (
                "name",
                "target",
                "state_path",
                "position_path",
                "mode",
                "default_open_min",
                "max_open_min",
            ):
                try:
                    raw[attr] = getattr(element, attr).value
                except (AttributeError, ValueError):
                    raw[attr] = None
            valves.append(build_valve(index, raw, taken))
        return valves


def export():
    ValveControlConfig.export(
        Path(__file__).parents[2] / "doover_config.json",
        "valve_control",
    )
