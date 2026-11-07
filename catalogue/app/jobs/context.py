"""Per-run context handed to source adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.fetch.client import Fetcher


@dataclass
class RunContext:
    fetcher: Fetcher
    #: 0 means uncapped. Held at 50 while parsers are being proven.
    max_items: int = 0
    watermark: str | None = None
    #: Adapter scratch space. DramaBox keeps its rotating buildId and category
    #: list here; nothing outside the adapter reads it.
    state: dict = field(default_factory=dict)
    on_progress: Callable[[int, int | None, str | None], None] | None = None

    _cancelled: bool = False
    _new_watermark: str | None = None

    def set_watermark(self, value: str) -> None:
        self._new_watermark = value

    @property
    def new_watermark(self) -> str | None:
        return self._new_watermark

    def progress(self, done: int, total: int | None = None, phase: str | None = None) -> None:
        if self.on_progress is not None:
            self.on_progress(done, total, phase)

    def cancel(self) -> None:
        self._cancelled = True

    def cancelled(self) -> bool:
        return self._cancelled
