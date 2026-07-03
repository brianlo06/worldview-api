"""Domain routers wired into the app by worldview_api.api."""

from . import (  # noqa: F401
    anomalies,
    ask,
    briefing,
    clusters,
    events,
    holo,
    markets,
    notepad,
    search,
    share,
    system,
    tts,
)

# Include order preserves the original single-file registration order.
ALL_ROUTERS = (
    system.router,
    anomalies.router,
    search.router,
    clusters.router,
    briefing.router,
    holo.router,
    tts.router,
    markets.router,
    events.router,
    ask.router,
    share.router,
    notepad.router,
)
