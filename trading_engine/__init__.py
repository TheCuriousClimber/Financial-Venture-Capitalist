"""Autonomous micro-account trading engine.

Design rule: everything in this package runs locally at zero token cost.
The only module allowed to spend Claude API credits is ``bridge.agent_trigger``.
"""
__version__ = "0.1.0"
