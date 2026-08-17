import pytest

from movie_theater_monitor import handler


def _config() -> handler.RuntimeConfig:
    return handler.RuntimeConfig.model_validate(
        {
            "THEATER_ID": "AANEM",
            "THEATER_NAME": "AMC Metreon 16",
            "FORMAT_SUBSTRING": "IMAX",
            "STATE_BUCKET": "state-bucket",
        }
    )


def _payload(ticketing_date: str) -> dict[str, object]:
    return {
        "viewModel": {
            "movies": [
                {
                    "id": 246093,
                    "title": "Movie One",
                    "variants": [
                        {
                            "amenityGroups": [
                                {
                                    "showtimes": [
                                        {
                                            "date": "6:00p",
                                            "expired": False,
                                            "ticketingDate": ticketing_date,
                                            "type": "Available",
                                            "showtimeHashCode": "first",
                                            "filmFormat": [{"filterName": "IMAX with Laser"}],
                                        }
                                    ]
                                }
                            ]
                        }
                    ],
                }
            ]
        }
    }


def test_accepts_numeric_provider_movie_ids() -> None:
    payload = handler.ShowtimePayload.model_validate(
        {
            "viewModel": {
                "movies": [
                    {
                        "id": 246093,
                        "title": "The Samurai and the Prisoner (2026)",
                        "variants": [],
                    }
                ]
            }
        }
    )

    assert payload.view_model.movies[0].movie_id == "246093"


def test_reads_calendar_dates_from_provider_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        handler,
        "fetch_json",
        lambda path, query: {"showtimeDates": ["2026-08-17", "2026-08-18"]},
    )

    assert handler.get_calendar_dates("AANEM") == ["2026-08-17", "2026-08-18"]


def test_treats_empty_calendar_as_provider_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "fetch_json", lambda path, query: {"showtimeDates": []})

    assert handler.get_calendar_dates("AANEM") is None


def test_treats_missing_calendar_dates_as_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handler, "fetch_json", lambda path, query: {"isEmpty": True})

    assert handler.get_calendar_dates("AANEM") is None


def test_records_showtime_time_as_parseable_datetime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "fetch_json", lambda path, query: _payload("2026-08-16+18:00"))

    records = handler.get_matching_showtimes_for_date(_config(), "2026-08-16")

    assert records is not None
    assert records[0].time == "2026-08-16T18:00:00"


def test_skips_showtimes_whose_ticketing_stamp_cannot_be_parsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handler, "fetch_json", lambda path, query: _payload("not-a-timestamp"))

    records = handler.get_matching_showtimes_for_date(_config(), "2026-08-16")

    assert records == []
