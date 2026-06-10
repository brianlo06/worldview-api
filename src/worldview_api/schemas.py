"""Pydantic request/response models for the public API."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class AnomalyOut(BaseModel):
    id: UUID
    region_code: str
    started_at: datetime
    last_seen_at: datetime
    peak_rate: float
    baseline_rate: float
    sigma_above: float
    pulse_lat: float | None = None
    pulse_lon: float | None = None
    driver_titles: list[str]


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    hours: int = Field(48, ge=1, le=720)
    limit: int = Field(30, ge=1, le=100)
    # below this we treat the result as off-topic
    min_similarity: float = Field(0.45, ge=0.0, le=1.0)


class SearchResultOut(BaseModel):
    cluster_id: UUID
    title: str
    summary: str | None = None
    url: str | None = None
    image_url: str | None = None
    source_outlet: str | None = None
    lat: float | None = None
    lon: float | None = None
    country_code: str | None = None
    city: str | None = None
    event_count: int
    category: str | None = None
    importance: float | None = None
    similarity: float
    breaking: bool = False
    geo_precision: str | None = None


class ClusterOut(BaseModel):
    """A cluster, surfaced as its representative event (image, headline, URL),
    with cluster context (event_count) for the 'N sources' indicator."""
    id: UUID
    # Representative event fields — picked from the cluster member nearest to
    # the centroid that has an image (or just nearest if none do)
    title: str
    summary: str | None = None
    url: str | None = None
    image_url: str | None = None
    source_outlet: str | None = None
    # Cluster context
    first_seen: datetime
    last_seen: datetime
    event_count: int
    lat: float | None = None
    lon: float | None = None
    country_code: str | None = None
    city: str | None = None
    category: str | None = None
    importance: float | None = None
    breaking: bool = False
    geo_precision: str | None = None


class ClusterMemberOut(BaseModel):
    id: UUID
    title: str
    summary: str | None = None
    url: str | None = None
    image_url: str | None = None
    source_outlet: str | None = None
    occurred_at: datetime
    categories: list[str]


class ClusterDetailOut(ClusterOut):
    members: list[ClusterMemberOut]


class MarketOut(BaseModel):
    symbol: str
    name: str
    city: str
    country_code: str | None = None
    lat: float
    lon: float
    price: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None
    currency: str | None = None
    updated_at: datetime


class EventOut(BaseModel):
    id: UUID
    title: str
    summary: str | None = None
    url: str | None = None
    image_url: str | None = None
    source_outlet: str | None = None
    occurred_at: datetime
    lat: float
    lon: float
    country_code: str | None = None
    city: str | None = None
    categories: list[str]
    importance: float | None = None
    breaking: bool = False
    geo_precision: str | None = None


class BriefingStoryOut(BaseModel):
    """One briefing story: its spoken narration plus the fields the client
    needs to fly the globe and render the selection card without a second
    round-trip to /clusters."""
    cluster_id: UUID
    narration: str
    title: str
    summary: str | None = None
    url: str | None = None
    image_url: str | None = None
    source_outlet: str | None = None
    lat: float | None = None
    lon: float | None = None
    country_code: str | None = None
    city: str | None = None
    category: str | None = None
    occurred_at: datetime | None = None


class BriefingResponse(BaseModel):
    intro: str
    stories: list[BriefingStoryOut]
    outro: str
    # "llm" when the narration was synthesized, "fallback" for the cleaned-up
    # no-LLM path (LLM disabled / over budget / failed).
    source: str = "fallback"


class AskRequest(BaseModel):
    question: str = Field("", max_length=500)
    lat: float | None = Field(None, ge=-90, le=90)
    lon: float | None = Field(None, ge=-180, le=180)


class AskResultItem(BaseModel):
    id: str | None = None
    title: str
    summary: str | None = None
    lat: float | None = None
    lon: float | None = None
    place: str | None = None
    source_outlet: str | None = None
    image_url: str | None = None
    country_code: str | None = None
    city: str | None = None


class AskResponse(BaseModel):
    answer: str
    place: str | None = None
    fly_lat: float | None = None
    fly_lon: float | None = None
    cluster_refs: list[str] = []
    results: list[AskResultItem] = []
    stats: dict = {}
    source: str = "live"


class ShareRequest(BaseModel):
    kind: str = Field("view", max_length=20)
    params: dict = {}
    title: str | None = Field(None, max_length=400)
    place: str | None = Field(None, max_length=200)
    question: str | None = Field(None, max_length=500)
    answer: str | None = Field(None, max_length=800)
    fly_lat: float | None = Field(None, ge=-90, le=90)
    fly_lon: float | None = Field(None, ge=-180, le=180)
    stats: dict = {}


class ShareResponse(BaseModel):
    id: str
    url: str
