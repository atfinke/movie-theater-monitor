"""Lambda runtime for polling a Fandango theater calendar with typed payloads."""

import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

import boto3
from botocore.exceptions import ClientError
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator

from movie_theater_monitor.monitor import (
    FilmFormat,
    MonitorState,
    ShowtimeRecord,
    format_alert,
    matches_format,
    provider_datetime,
)

FANDANGO_ORIGIN = "https://www.fandango.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/26.4 Safari/605.1.15"
)


class JsonFormatter(logging.Formatter):
    """Render monitor events as one searchable JSON record per log line."""

    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "monitor_event", record.getMessage())
        fields = getattr(record, "monitor_fields", {})
        return json.dumps({"event": event, "level": record.levelname, **fields}, sort_keys=True)


logger = logging.getLogger("movie_theater_monitor")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def log_event(level: int, event: str, **fields: str | int | bool | None) -> None:
    """Log only operational metadata, never endpoints, state contents, or SNS targets."""

    logger.log(level, "monitor_event", extra={"monitor_event": event, "monitor_fields": fields})


class RuntimeConfig(BaseModel):
    """Environment-backed configuration for one theater/format monitor."""

    model_config = ConfigDict(populate_by_name=True)

    theater_id: str = Field(validation_alias="THEATER_ID")
    theater_name: str = Field(validation_alias=AliasChoices("THEATER_NAME", "THEATER_ID"))
    format_substring: str = Field(validation_alias=AliasChoices("FORMAT_SUBSTRING", "FORMAT"))
    state_bucket: str = Field(validation_alias="STATE_BUCKET")
    state_key: str = Field(default="movie-theater-monitor/state.json", validation_alias="STATE_KEY")
    sns_topic_arn: str | None = Field(default=None, validation_alias="SNS_TOPIC_ARN")
    booking_url: str = Field(default=FANDANGO_ORIGIN, validation_alias="BOOKING_URL")

    @classmethod
    def from_environment(cls) -> "RuntimeConfig":
        return cls.model_validate(os.environ)


class CalendarPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    showtime_dates: list[str] = Field(default_factory=list, validation_alias="showtimeDates")


class ProviderShowtime(BaseModel):
    model_config = ConfigDict(extra="ignore")

    expired: bool = False
    film_format: list[FilmFormat] = Field(default_factory=list, validation_alias="filmFormat")
    ticketing_date: str | None = Field(default=None, validation_alias="ticketingDate")
    date: str | None = None
    status: str | None = Field(default=None, validation_alias="type")
    showtime_hash_code: str | None = Field(default=None, validation_alias="showtimeHashCode")


class AmenityGroup(BaseModel):
    model_config = ConfigDict(extra="ignore")

    showtimes: list[ProviderShowtime] = Field(default_factory=list)


class MovieVariant(BaseModel):
    model_config = ConfigDict(extra="ignore")

    amenity_groups: list[AmenityGroup] = Field(
        default_factory=list,
        validation_alias="amenityGroups",
    )


class ProviderMovie(BaseModel):
    model_config = ConfigDict(extra="ignore")

    movie_id: str = Field(validation_alias="id")
    title: str
    variants: list[MovieVariant] = Field(default_factory=list)

    @field_validator("movie_id", mode="before")
    @classmethod
    def normalize_movie_id(cls, value: str | int) -> str:
        """Accept either the string or numeric identifier the provider has used."""

        return str(value)


class ShowtimeViewModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    movies: list[ProviderMovie] = Field(default_factory=list)


class ShowtimePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    view_model: ShowtimeViewModel = Field(validation_alias="viewModel")


class AwsErrorPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str = Field(validation_alias="Code")


class AwsClientErrorPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    error: AwsErrorPayload = Field(validation_alias="Error")


class S3ObjectResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    body: Any = Field(validation_alias="Body")


class InvocationEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    full_rescan: bool = False


def fetch_json(path: str, query: Mapping[str, str | None]) -> object | None:
    """Fetch one public Fandango endpoint, returning None only for provider failure."""

    query_string = urlencode({key: value for key, value in query.items() if value is not None})
    url = f"{FANDANGO_ORIGIN}{path}"
    if query_string:
        url = f"{url}?{query_string}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"{FANDANGO_ORIGIN}/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        log_event(logging.WARNING, "provider_http_error", path=path, status_code=error.code)
    except (OSError, json.JSONDecodeError) as error:
        log_event(
            logging.WARNING,
            "provider_request_failed",
            path=path,
            error_type=type(error).__name__,
        )
    return None


def get_calendar_dates(theater_id: str) -> list[str] | None:
    """Get visible calendar dates; None means the provider request was unsuccessful."""

    payload = fetch_json(f"/napi/theaterCalendar/{theater_id}", {})
    if payload is None:
        return None
    try:
        showtime_dates = CalendarPayload.model_validate(payload).showtime_dates
    except ValidationError as error:
        log_event(logging.WARNING, "provider_calendar_invalid", error_type=type(error).__name__)
        return None

    if not showtime_dates:
        # An empty calendar validates cleanly but is indistinguishable from a provider
        # outage, and treating it as truth would erase every tracked showtime.
        log_event(logging.WARNING, "provider_calendar_empty")
        return None
    return showtime_dates


def get_matching_showtimes_for_date(
    config: RuntimeConfig,
    date: str,
) -> list[ShowtimeRecord] | None:
    """Return typed matching showtimes for a date, or None when it was not safely scanned."""

    payload = fetch_json(
        f"/napi/theaterMovieShowtimes/{config.theater_id}",
        {"startDate": date, "isdesktop": "true", "partnerRestrictedTicketing": ""},
    )
    if payload is None:
        return None
    try:
        movies = ShowtimePayload.model_validate(payload).view_model.movies
    except ValidationError as error:
        log_event(logging.WARNING, "provider_showtimes_invalid", error_type=type(error).__name__)
        return None

    records: list[ShowtimeRecord] = []
    skipped_incomplete = 0
    for movie in movies:
        for variant in movie.variants:
            for amenity_group in variant.amenity_groups:
                for showtime in amenity_group.showtimes:
                    format_names = [film_format.filter_name for film_format in showtime.film_format]
                    if showtime.expired or not matches_format(
                        format_names,
                        config.format_substring,
                    ):
                        continue
                    if showtime.ticketing_date is None or showtime.date is None:
                        skipped_incomplete += 1
                        continue
                    try:
                        showtime_time = provider_datetime(showtime.ticketing_date)
                    except ValueError:
                        skipped_incomplete += 1
                        continue
                    records.append(
                        ShowtimeRecord(
                            movie_id=movie.movie_id,
                            title=movie.title,
                            date=date,
                            time=showtime_time,
                            ticketing_date=showtime.ticketing_date,
                            status=showtime.status,
                            formats=format_names,
                            showtime_hash_code=showtime.showtime_hash_code,
                        )
                    )
    log_event(
        logging.INFO,
        "date_scan_completed",
        date=date,
        matching_showtimes=len(records),
        skipped_incomplete=skipped_incomplete,
    )
    return records


def load_state(s3_client: Any, config: RuntimeConfig) -> MonitorState:
    """Load prior state and retain compatibility with legacy record serialization."""

    try:
        object_response = S3ObjectResponse.model_validate(
            s3_client.get_object(Bucket=config.state_bucket, Key=config.state_key)
        )
        state = MonitorState.model_validate_json(object_response.body.read())
        log_event(
            logging.INFO,
            "state_loaded",
            known_dates=len(state.known_dates),
            showtimes=len(state.showtimes),
        )
        return state
    except ClientError as error:
        error_payload = AwsClientErrorPayload.model_validate(error.response)
        if error_payload.error.code == "NoSuchKey":
            log_event(logging.INFO, "state_missing")
            return MonitorState()
        log_event(logging.ERROR, "state_load_failed", error_code=error_payload.error.code)
        raise
    except ValidationError:
        log_event(logging.ERROR, "state_invalid")
        raise


def save_state(s3_client: Any, config: RuntimeConfig, state: MonitorState) -> None:
    """Write only fully successful scan results."""

    s3_client.put_object(
        Bucket=config.state_bucket,
        Key=config.state_key,
        Body=state.model_dump_json().encode("utf-8"),
        ContentType="application/json",
    )
    log_event(
        logging.INFO,
        "state_saved",
        known_dates=len(state.known_dates),
        showtimes=len(state.showtimes),
    )


def notify(sns_client: Any, config: RuntimeConfig, records: list[ShowtimeRecord]) -> str:
    """Publish one aggregated alert, or log the deliberate no-topic dry run."""

    subject = f"New {config.format_substring} showtimes: {config.theater_name}"
    message = format_alert(
        config.theater_name,
        config.format_substring,
        records,
        config.booking_url,
    )
    if config.sns_topic_arn is None:
        log_event(logging.WARNING, "alert_skipped_no_topic", new_showtimes=len(records))
        return message
    sns_client.publish(TopicArn=config.sns_topic_arn, Subject=subject[:100], Message=message)
    log_event(
        logging.INFO,
        "alert_published",
        new_showtimes=len(records),
        movies=len({record.title for record in records}),
    )
    return message


def handler(event: object, _context: object) -> dict[str, str | int | bool]:
    """Run one incremental or full monitor scan without erasing state on failures."""

    config = RuntimeConfig.from_environment()
    scan_event = InvocationEvent.model_validate(event if isinstance(event, Mapping) else {})
    full_rescan = scan_event.full_rescan
    scan_mode = "full" if full_rescan else "incremental"
    log_event(logging.INFO, "scan_started", scan_mode=scan_mode, theater_id=config.theater_id)

    s3_client = boto3.client("s3")
    sns_client = boto3.client("sns")
    previous_state = load_state(s3_client, config)
    calendar_dates = get_calendar_dates(config.theater_id)
    if calendar_dates is None:
        log_event(
            logging.ERROR,
            "scan_aborted_provider_failure",
            scan_mode=scan_mode,
            stage="calendar",
        )
        return {"scanMode": scan_mode, "providerOutcome": "calendar_failed", "stateSaved": False}

    previous_dates = set(previous_state.known_dates)
    new_dates = sorted(set(calendar_dates).difference(previous_dates))
    dates_to_scan = calendar_dates if full_rescan else new_dates
    scanned_records: dict[str, ShowtimeRecord] = {}
    for date in dates_to_scan:
        records = get_matching_showtimes_for_date(config, date)
        if records is None:
            log_event(
                logging.ERROR,
                "scan_aborted_provider_failure",
                scan_mode=scan_mode,
                stage="showtimes",
            )
            return {
                "scanMode": scan_mode,
                "providerOutcome": "showtimes_failed",
                "stateSaved": False,
            }
        scanned_records.update({record.key: record for record in records})

    if full_rescan:
        current_showtimes = scanned_records
    else:
        current_showtimes = {
            key: record
            for key, record in previous_state.showtimes.items()
            if record.date in calendar_dates
        }
        current_showtimes.update(scanned_records)

    new_keys = sorted(set(current_showtimes).difference(previous_state.showtimes))
    removed_keys = sorted(set(previous_state.showtimes).difference(current_showtimes))
    log_event(
        logging.INFO,
        "state_delta",
        scan_mode=scan_mode,
        new_dates=len(new_dates),
        new_showtimes=len(new_keys),
        removed_showtimes=len(removed_keys),
        tracked_showtimes=len(current_showtimes),
    )
    if new_keys:
        notify(sns_client, config, [current_showtimes[key] for key in new_keys])
    else:
        log_event(logging.INFO, "alert_not_needed", scan_mode=scan_mode)

    state = MonitorState(known_dates=calendar_dates, showtimes=current_showtimes)
    save_state(s3_client, config, state)
    return {
        "scanMode": scan_mode,
        "providerOutcome": "success",
        "stateSaved": True,
        "newDatesChecked": len(new_dates),
        "newShowtimes": len(new_keys),
        "removedShowtimes": len(removed_keys),
        "trackedShowtimes": len(current_showtimes),
    }
