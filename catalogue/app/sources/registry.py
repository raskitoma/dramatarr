"""The one list of source adapters.

Previously each caller kept its own `SOURCE_MODULES` dict -- the runner, the
CLI, the image job and the robots check all had a copy. Adding a source meant
finding all four, and the copies had already begun to differ.

More importantly, `source` rows used to be created only as a side effect of
running a sync. On a fresh deployment that meant nothing existed for the
scheduler to schedule or the API to trigger: `create_run` answered "unknown
source", `tick()` reported nothing due, and the instance sat inert with no
error. Registration now happens at startup, from this list.
"""

from __future__ import annotations

import importlib
from types import ModuleType

from sqlalchemy.orm import Session

#: key -> module path. Order is the order they appear in the console.
ADAPTERS: dict[str, str] = {
    "dramabox": "app.sources.dramabox",
    "reelshort": "app.sources.reelshort",
    "goodshort": "app.sources.goodshort",
    "netshort": "app.sources.netshort",
    "imdb": "app.sources.imdb",
}

#: Sources that crawl a catalogue. IMDb is an enricher: it has no watch URL and
#: is never the subject of a catalogue sync.
PLATFORM_KEYS = ("dramabox", "reelshort", "goodshort", "netshort")


def load(key: str) -> ModuleType:
    return importlib.import_module(ADAPTERS[key])


def register_all(session: Session) -> list[str]:
    """Ensure a `source` row exists for every adapter. Idempotent.

    Called at startup so a freshly deployed instance is immediately usable:
    the scheduler has something to schedule, the API has something to trigger,
    and the dashboard is not empty.
    """
    from app.ingest import ensure_source

    registered = []
    for key in ADAPTERS:
        module = load(key)
        ensure_source(
            session,
            module.KEY,
            module.STUDIO_NAME,
            module.BASE_URL,
            module.WATCH_URL_TEMPLATE,
            kind=getattr(module, "KIND", "platform"),
        )
        registered.append(key)
    session.commit()
    return registered
