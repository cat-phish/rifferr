"""Public plugin entry-point group names.

Built-ins register through these groups exactly like third-party plugins
(AGENTS.md non-negotiable 7). The names are public API: don't rename them.
"""

from typing import Final

STEPS: Final = "rifferr.steps"
COVER_PROVIDERS: Final = "rifferr.cover_providers"
AI_PROVIDERS: Final = "rifferr.ai_providers"
TEMPLATE_FUNCTIONS: Final = "rifferr.template_functions"

ALL_GROUPS: Final = (STEPS, COVER_PROVIDERS, AI_PROVIDERS, TEMPLATE_FUNCTIONS)
