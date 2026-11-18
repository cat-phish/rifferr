"""The ``rifferr`` beets plugin: Rifferr template functions (C-TEMPLATE-FUNCTIONS).

beets imports plugins as ``beetsplug.<name>`` and only accepts a plugin class defined
in that module, so this shim subclasses the real plugin. Everything else lives in
``rifferr.backends.beets.templates``.
"""

from rifferr.backends.beets.templates import RifferrTemplatePlugin


class RifferrPlugin(RifferrTemplatePlugin):
    pass
