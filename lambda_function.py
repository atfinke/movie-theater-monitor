"""AWS Lambda entrypoint retained for the deployed function's handler setting."""

from movie_theater_monitor.handler import handler

__all__ = ["handler"]
