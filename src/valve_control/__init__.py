from typing import Any

from pydoover.processor import run_app

from .application import ValveControlApplication


def handler(event: dict[str, Any], context: Any):
    """Lambda handler entry point."""
    return run_app(ValveControlApplication(), event, context)
