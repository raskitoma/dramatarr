"""Shared Pydantic base.

Every response model inherits `APIModel`, which is what makes the wire format camelCase
while Python stays snake_case. Request models accept either spelling.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict


def to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(word.capitalize() for word in tail)


class APIModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
        ser_json_timedelta="float",
    )


T = TypeVar("T")


class Page(APIModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.page_size)) if self.page_size else 1


class Ok(APIModel):
    ok: bool = True


__all__ = ["APIModel", "Ok", "Page", "to_camel"]
