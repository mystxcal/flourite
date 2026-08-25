"""Flourite.

A high-value, event-sourced orchestration runtime for Codex-backed problem solving.
"""

__version__ = "0.6.0"

from .engine import FrontierEngine
from .models import GoalContract, RunState

__all__ = ["FrontierEngine", "GoalContract", "RunState"]
