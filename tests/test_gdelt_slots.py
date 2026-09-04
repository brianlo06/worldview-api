"""Tests for the translingual feed's published-slot walk-back.

Run with: `cd worldview-api && .venv/bin/python -m unittest tests.test_gdelt_slots`
"""

from __future__ import annotations

import unittest
from unittest import mock

import httpx

from worldview_api.ingest.common import first_available

BASE = "https://data.gdeltproject.org/gdeltv2/{stamp}.translation.gkg.csv.zip"
LATEST = BASE.format(stamp="20260904203000")


def _head(published: set[str]):
    """Fake httpx.head where only `published` stamps return 200."""

    def _inner(url: str, **_kwargs: object) -> httpx.Response:
        stamp = url.split("/")[-1].split(".")[0]
        return httpx.Response(200 if stamp in published else 404)

    return _inner


class FirstAvailableTests(unittest.TestCase):
    def test_returns_url_unchanged_when_already_published(self) -> None:
        with mock.patch("httpx.head", _head({"20260904203000"})):
            self.assertEqual(first_available(LATEST), LATEST)

    def test_walks_back_in_fifteen_minute_slots(self) -> None:
        # GDELT publishes the translingual GKG up to ~45 min behind its index.
        with mock.patch("httpx.head", _head({"20260904194500"})):
            self.assertEqual(
                first_available(LATEST), BASE.format(stamp="20260904194500")
            )

    def test_returns_none_when_window_is_empty(self) -> None:
        with mock.patch("httpx.head", _head(set())):
            self.assertIsNone(first_available(LATEST))

    def test_stops_at_max_back(self) -> None:
        # 20:30 back 6 slots is 19:00; 18:45 is the 7th and out of window.
        with mock.patch("httpx.head", _head({"20260904184500"})):
            self.assertIsNone(first_available(LATEST))

    def test_network_error_on_one_slot_does_not_abort_the_walk(self) -> None:
        def flaky(url: str, **_kwargs: object) -> httpx.Response:
            stamp = url.split("/")[-1].split(".")[0]
            if stamp == "20260904203000":
                raise httpx.ConnectError("boom")
            return httpx.Response(200 if stamp == "20260904201500" else 404)

        with mock.patch("httpx.head", flaky):
            self.assertEqual(
                first_available(LATEST), BASE.format(stamp="20260904201500")
            )

    def test_url_without_a_timestamp_is_returned_as_is(self) -> None:
        odd = "https://data.gdeltproject.org/gdeltv2/latest.zip"
        self.assertEqual(first_available(odd), odd)


if __name__ == "__main__":
    unittest.main()
