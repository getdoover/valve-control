from pathlib import Path

from pydoover import notifications as n


class ValveControlNotifications(n.Notifications):
    command_not_confirmed = n.Notification(
        "A valve command was not confirmed",
        description=(
            "An open or close command was sent but the valve did not report the commanded "
            "state within the confirmation timeout."
        ),
        severity=n.NotificationSeverity.Warn,
    )
    open_too_long = n.Notification(
        "A valve has been open longer than its maximum",
        description="The valve has reported open for longer than its Max Open Duration.",
        severity=n.NotificationSeverity.Warn,
    )


def export():
    ValveControlNotifications.export(
        Path(__file__).parents[2] / "doover_config.json", "valve_control"
    )
