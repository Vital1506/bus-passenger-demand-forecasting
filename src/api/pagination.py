"""Shared pagination for large historical queries.

Every list endpoint in :mod:`src.api` uses the same envelope so clients can page
through years of observations without loading a whole store into memory.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Generic, Iterable, Sequence, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

T = TypeVar("T")


@dataclass(frozen=True)
class PageParams:
    """Requested slice, validated by FastAPI's ``Query`` constraints."""

    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def page_params(
    page: int = Query(default=1, ge=1, le=100_000, description="1-based page number."),
    page_size: int = Query(
        default=DEFAULT_PAGE_SIZE,
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"Rows per page (max {MAX_PAGE_SIZE}).",
    ),
) -> PageParams:
    """Dependency that normalizes ``page``/``page_size`` query parameters."""

    return PageParams(page=page, page_size=page_size)


class Page(BaseModel, Generic[T]):
    """A single page of results plus the counters a client needs to continue."""

    items: list[T] = Field(default_factory=list)
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    total: int = 0
    pages: int = 0
    has_next: bool = False
    has_previous: bool = False

    @classmethod
    def build(cls, items: Sequence[T], params: PageParams) -> "Page[T]":
        total = len(items)
        pages = math.ceil(total / params.page_size) if total else 0
        start = params.offset
        window = list(items[start : start + params.page_size])
        return cls(
            items=window,
            page=params.page,
            page_size=params.page_size,
            total=total,
            pages=pages,
            has_next=params.page < pages,
            has_previous=params.page > 1 and total > 0,
        )


def slice_page(items: Iterable[T], params: PageParams) -> tuple[list[T], int]:
    """Materialize ``items`` once and return the requested window plus total."""

    materialized = list(items)
    return list(materialized[params.offset : params.offset + params.page_size]), len(materialized)
