"""Boundary-policy generators of the agent governance extension
(Appendix G, G.10): the code targets ``agent-rego`` and ``agent-cedar``."""

from .cedar_gen import generate_agent_cedar
from .rego_gen import generate_agent_rego

__all__ = ["generate_agent_rego", "generate_agent_cedar"]
