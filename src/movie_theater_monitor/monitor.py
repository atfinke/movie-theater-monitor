"""Typed domain models and comparison helpers for showtime monitoring."""

from collections.abc import Collection
from datetime import datetime
from itertools import groupby

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class FilmFormat(BaseModel):
    """One format label returned by the Fandango showtime API."""

    model_config = ConfigDict(populate_by_name=True)

    filter_name: str = Field(validation_alias="filterName")


class ShowtimeRecord(BaseModel):
    """The stable, minimal record persisted for a discovered showtime."""

    model_config = ConfigDict(populate_by_name=True)

    movie_id: str = Field(validation_alias=AliasChoices("movie_id", "movieId"))
    title: str
    date: str
    time: str
    ticketing_date: str = Field(validation_alias=AliasChoices("ticketing_date", "ticketingDate"))
    status: str | None
    formats: list[str]
    showtime_hash_code: str | None = Field(
        validation_alias=AliasChoices("showtime_hash_code", "showtimeHashCode")
    )

    @field_validator("movie_id", mode="before")
    @classmethod
    def normalize_movie_id(cls, value: str | int) -> str:
        """Accept the numeric identifier used by the deployed legacy state."""

        return str(value)

    @property
    def key(self) -> str:
        return f"{self.movie_id}|{self.date}|{self.ticketing_date}"


class MonitorState(BaseModel):
    """Persisted monitor state for calendar and showtime change detection."""

    known_dates: list[str] = Field(default_factory=list)
    showtimes: dict[str, ShowtimeRecord] = Field(default_factory=dict)


def matches_format(formats: Collection[str], format_substring: str) -> bool:
    """Return whether a showtime has a format containing the configured label."""

    normalized_filter = format_substring.casefold()
    return any(normalized_filter in format_name.casefold() for format_name in formats)


def is_imax_showtime(formats: Collection[str], format_substring: str) -> bool:
    """Compatibility wrapper for callers using the original helper name."""

    return matches_format(formats, format_substring)


def new_showtime_keys(current: Collection[str], previous: Collection[str]) -> list[str]:
    """Return deterministically ordered showtime identifiers not seen before."""

    return sorted(set(current).difference(previous))


def format_alert(
    theater_name: str,
    format_substring: str,
    records: Collection[ShowtimeRecord],
    booking_url: str,
) -> str:
    """Format newly discovered showtimes into one mobile-friendly email body."""

    ordered_records = sorted(records, key=lambda record: (record.title, record.time))
    lines = [f"New {format_substring} showtimes at {theater_name}", ""]
    for title, movie_records in groupby(ordered_records, key=lambda record: record.title):
        lines.append(title)
        for record in movie_records:
            local_time = datetime.fromisoformat(record.time)
            date_label = f"{local_time.strftime('%A, %B')} {local_time.day}"
            time_label = local_time.strftime("%I:%M %p").lstrip("0")
            lines.append(f"• {date_label} at {time_label}")
        lines.append("")
    lines.extend(["Book tickets:", booking_url])
    return "\n".join(lines)
