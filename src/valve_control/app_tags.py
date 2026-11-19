"""Tags. Each valve gets its own set, prefixed with its key (its target id, made safe):
e.g. flood_gate_1_state, flood_gate_1_last_changed, flood_gate_1_pending,
flood_gate_1_open_until. They are added at setup from the config, since the valves are."""

from pydoover.tags import Tag, Tags

# name -> (type, default). The first group is the public state; the rest carries the
# app's own bookkeeping between invocations and drives its page.
VALVE_TAGS: dict[str, tuple[str, object]] = {
    "state": ("string", None),  # "open" / "closed"; None until the first reading
    "last_changed": ("number", None),  # ms since epoch
    "pending": ("string", None),  # "open" / "close" while awaiting confirmation
    "open_until": ("number", None),  # ms; when a timed open is due to close
    "position": ("number", None),  # %, when a position input is configured
    "state_text": ("string", "Unknown"),
    "pending_since": ("number", None),
    "pending_request_id": ("string", None),
    "pending_duration_s": ("number", None),
    "problem": ("string", None),
    "last_command": ("string", "None sent yet"),
    "timeout_notified": ("boolean", False),
    "overrun_notified": ("boolean", False),
    "warning": ("string", ""),
    "warning_hidden": ("boolean", True),
    "open_until_hidden": ("boolean", True),
    # Both controls show until a trusted reading says which one applies.
    "open_hidden": ("boolean", False),
    "close_hidden": ("boolean", False),
}


def tag_name(key: str, field: str) -> str:
    return f"{key}_{field}"


class ValveControlTags(Tags):
    open_count = Tag(
        "number", default=None
    )  # valves reporting open, of those with a reading

    async def setup(self):
        valves = self.config.valve_list() if self.config is not None else []
        for valve in valves:
            for field, (tag_type, default) in VALVE_TAGS.items():
                name = tag_name(valve.key, field)
                if self._get_declaration(name) is None:
                    self.add_tag(name, Tag(tag_type, default=default, name=name))
