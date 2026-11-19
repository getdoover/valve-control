"""The page: one panel per configured valve, built at setup from the config.

Only the control that changes the reported state is shown (Open while Closed, Close
while Open); both show while the state is unknown, pending or after a failed command.
Every value shown is a tag the processor keeps, so the panels read cleanly with no data
("Unknown", empty times) and update as readings arrive. Each control is named
`<valve key>__<action>`, which application.py routes back to its valve.
"""

from pathlib import Path

from pydoover import ui

from .app_tags import tag_name
from .model import Valve

OPEN_FOR = "open_for"
OPEN = "open"
CLOSE = "close"


def element_name(key: str, action: str) -> str:
    return f"{key}__{action}"


class ValveControlUI(ui.UI):
    async def setup(self):
        if self.config is None:
            return
        open_label = self.config.open_label.value or "Open"
        close_label = self.config.close_label.value or "Close"
        for index, valve in enumerate(self.config.valve_list()):
            self.add_element(self._panel(valve, index, open_label, close_label))

    def _tag(self, valve: Valve, field: str):
        return ui.tag_ref(self.tags.get(tag_name(valve.key, field)))

    def _panel(self, valve: Valve, index: int, open_label: str, close_label: str):
        k = valve.key
        children = [
            ui.WarningIndicator(
                self._tag(valve, "warning"),
                name=element_name(k, "warning"),
                hidden=self._tag(valve, "warning_hidden"),
                can_cancel=False,
                position=1,
            ),
            ui.TextVariable(
                "State",
                value=self._tag(valve, "state_text"),
                name=element_name(k, "state"),
                position=2,
            ),
        ]
        if valve.position_path is not None:
            children.append(
                ui.NumericVariable(
                    "Position",
                    value=self._tag(valve, "position"),
                    units="%",
                    precision=0,
                    name=element_name(k, "position"),
                    position=3,
                )
            )
        children += [
            ui.Timestamp(
                "Closes",
                value=self._tag(valve, "open_until"),
                precision="second",
                hidden=self._tag(valve, "open_until_hidden"),
                name=element_name(k, "open_until"),
                position=4,
            ),
            ui.Timestamp(
                "Last Changed",
                value=self._tag(valve, "last_changed"),
                name=element_name(k, "last_changed"),
                position=5,
            ),
            ui.TextVariable(
                "Last Command",
                value=self._tag(valve, "last_command"),
                name=element_name(k, "last_command"),
                position=6,
            ),
        ]
        if valve.timed:
            children.append(
                ui.FloatInput(
                    f"{open_label} For (min)",
                    name=element_name(k, OPEN_FOR),
                    min_val=0,
                    max_val=valve.max_open_min if valve.max_open_min > 0 else ui.NotSet,
                    default=valve.default_open_min,
                    requires_confirm=True,
                    hidden=self._tag(valve, "open_hidden"),
                    position=7,
                )
            )
        else:
            children.append(
                ui.Button(
                    open_label,
                    name=element_name(k, OPEN),
                    requires_confirm=True,
                    hidden=self._tag(valve, "open_hidden"),
                    position=7,
                )
            )
        children.append(
            ui.Button(
                close_label,
                name=element_name(k, CLOSE),
                requires_confirm=True,
                hidden=self._tag(valve, "close_hidden"),
                position=8,
            )
        )
        return ui.Submodule(
            valve.name,
            children=children,
            status=self._tag(valve, "state_text"),
            is_collapsed=False,
            name=element_name(k, "panel"),
            position=10 + index,
        )


def export():
    ValveControlUI(None, None, None).export(
        Path(__file__).parents[2] / "doover_config.json",
        "valve_control",
    )
