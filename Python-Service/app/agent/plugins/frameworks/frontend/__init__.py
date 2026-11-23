"""Angular, React, and Vue framework plugins (one shared extractor, three thin plugins)."""

from agent.plugins.frameworks.frontend.plugin import (
    AngularFrameworkPlugin,
    ReactFrameworkPlugin,
    VueFrameworkPlugin,
)

__all__ = ["AngularFrameworkPlugin", "ReactFrameworkPlugin", "VueFrameworkPlugin"]
