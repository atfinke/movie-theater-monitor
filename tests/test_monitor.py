from movie_theater_monitor import monitor


def test_classifies_imax_format_case_insensitively() -> None:
    assert monitor.is_imax_showtime(["IMAX with Laser"], "imax")
    assert not monitor.is_imax_showtime(["Dolby Cinema"], "imax")


def test_detects_only_unseen_showtimes() -> None:
    seen = {"movie-1|2026-09-01|2026-09-01T19:00:00-07:00"}
    current = {
        *seen,
        "movie-1|2026-09-02|2026-09-02T19:00:00-07:00",
    }

    assert monitor.new_showtime_keys(current, seen) == [
        "movie-1|2026-09-02|2026-09-02T19:00:00-07:00"
    ]


def test_loads_existing_camel_case_showtime_records() -> None:
    state = monitor.MonitorState.model_validate(
        {
            "known_dates": ["2026-09-04"],
            "showtimes": {
                "movie-1|2026-09-04|2026-09-04T19:00:00-07:00": {
                    "movieId": 123,
                    "title": "Movie One",
                    "date": "2026-09-04",
                    "time": "2026-09-04T19:00:00-07:00",
                    "ticketingDate": "2026-09-04T19:00:00-07:00",
                    "status": "Available",
                    "formats": ["IMAX with Laser"],
                    "showtimeHashCode": "first",
                }
            },
        }
    )

    assert state.showtimes[next(iter(state.showtimes))].movie_id == "123"


def test_groups_multiple_movies_in_one_actionable_alert() -> None:
    records = [
        monitor.ShowtimeRecord(
            movie_id="movie-1",
            title="Movie One",
            date="2026-09-04",
            time="2026-09-04T19:00:00-07:00",
            ticketing_date="2026-09-04T19:00:00-07:00",
            status="Available",
            formats=["IMAX with Laser"],
            showtime_hash_code="first",
        ),
        monitor.ShowtimeRecord(
            movie_id="movie-2",
            title="Movie Two",
            date="2026-09-05",
            time="2026-09-05T14:00:00-07:00",
            ticketing_date="2026-09-05T14:00:00-07:00",
            status="Available",
            formats=["IMAX with Laser"],
            showtime_hash_code="second",
        ),
    ]

    message = monitor.format_alert(
        "AMC Metreon 16",
        "IMAX",
        records,
        "https://www.amctheatres.com/movie-theatres/san-francisco/amc-metreon-16",
    )

    assert "Movie One" in message
    assert "Movie Two" in message
    assert "Friday, September 4 at 7:00 PM" in message
    assert "Saturday, September 5 at 2:00 PM" in message
    assert "amctheatres.com/movie-theatres/san-francisco/amc-metreon-16" in message
