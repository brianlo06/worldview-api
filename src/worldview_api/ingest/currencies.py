"""Frankfurter FX rate ingestion.

USD-base rates for major world currencies, pinned to their respective
financial centers, with day-over-day change_pct so the frontend can
color them green/red exactly like stock indices.

Frankfurter is free, no API key, backed by ECB reference rates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx
from psycopg.types.json import Jsonb

from ..db import get_pool

log = logging.getLogger(__name__)

FRANKFURTER_URL = "https://api.frankfurter.dev/v1/{start}..{end}"


@dataclass(frozen=True)
class CurrencyPin:
    code: str
    name: str
    city: str
    country_code: str
    lat: float
    lon: float


# Major currencies pinned to canonical financial centers.
# Slight lat offset (−0.6°) so currency dots don't sit exactly on top of
# market dots at the same city.
CURRENCY_PINS: tuple[CurrencyPin, ...] = (
    CurrencyPin("EUR", "Euro",                 "Frankfurt",   "DE", 49.50,   8.68),
    CurrencyPin("GBP", "British Pound",        "London",      "GB", 50.91,  -0.13),
    CurrencyPin("JPY", "Japanese Yen",         "Tokyo",       "JP", 35.08, 139.69),
    CurrencyPin("CNY", "Chinese Yuan",         "Shanghai",    "CN", 30.63, 121.47),
    CurrencyPin("CHF", "Swiss Franc",          "Zurich",      "CH", 46.77,   8.55),
    CurrencyPin("CAD", "Canadian Dollar",      "Toronto",     "CA", 43.05, -79.38),
    CurrencyPin("AUD", "Australian Dollar",    "Sydney",      "AU", -34.47, 151.21),
    CurrencyPin("INR", "Indian Rupee",         "Mumbai",      "IN", 18.48,  72.88),
    CurrencyPin("KRW", "South Korean Won",     "Seoul",       "KR", 36.97, 126.98),
    CurrencyPin("BRL", "Brazilian Real",       "São Paulo",   "BR", -24.15, -46.63),
    CurrencyPin("MXN", "Mexican Peso",         "Mexico City", "MX", 18.83, -99.13),
    CurrencyPin("ZAR", "South African Rand",   "Johannesburg","ZA", -26.80, 28.04),
    CurrencyPin("SGD", "Singapore Dollar",     "Singapore",   "SG",  0.75, 103.82),
    CurrencyPin("HKD", "Hong Kong Dollar",     "Hong Kong",   "HK", 21.70, 114.18),
    CurrencyPin("NOK", "Norwegian Krone",      "Oslo",        "NO", 59.31,  10.75),
    CurrencyPin("SEK", "Swedish Krona",        "Stockholm",   "SE", 58.73,  18.07),
    CurrencyPin("TRY", "Turkish Lira",         "Istanbul",    "TR", 40.41,  28.98),
)


def fetch_fx_window() -> dict[str, tuple[float, float]]:
    """Return {currency_code: (latest_rate, previous_rate)} for USD-base FX.

    Queries Frankfurter for a small date window ending today and picks the
    two most recent dates with data, so the result tolerates weekends and
    holidays where rates aren't published.
    """
    today = date.today()
    start = today - timedelta(days=6)
    symbols = ",".join(p.code for p in CURRENCY_PINS)
    url = FRANKFURTER_URL.format(start=start.isoformat(), end=today.isoformat())
    r = httpx.get(url, params={"base": "USD", "symbols": symbols}, timeout=20)
    r.raise_for_status()
    payload = r.json()
    rates_by_date: dict[str, dict[str, float]] = payload.get("rates", {})
    if not rates_by_date:
        return {}

    sorted_dates = sorted(rates_by_date.keys())
    if len(sorted_dates) < 2:
        return {}
    latest = rates_by_date[sorted_dates[-1]]
    previous = rates_by_date[sorted_dates[-2]]

    result: dict[str, tuple[float, float]] = {}
    for code in latest:
        if code in previous:
            result[code] = (float(latest[code]), float(previous[code]))
    return result


def ingest_currencies_once() -> dict[str, int | str]:
    rates = fetch_fx_window()
    if not rates:
        return {"status": "no_data", "fetched": 0}

    pool = get_pool()
    fetched = 0
    skipped = 0

    with pool.connection() as conn, conn.cursor() as cur:
        for pin in CURRENCY_PINS:
            pair = rates.get(pin.code)
            if pair is None:
                skipped += 1
                continue
            latest, previous = pair
            change_pct = (latest - previous) / previous * 100.0 if previous else 0.0
            symbol = f"FX:USD/{pin.code}"
            raw_payload = {
                "code": pin.code,
                "latest_rate_per_usd": latest,
                "previous_rate_per_usd": previous,
                "as_of": datetime.now(timezone.utc).isoformat(),
            }
            cur.execute(
                """
                INSERT INTO markets (
                    symbol, name, city, country_code,
                    location, price, prev_close, change_pct, currency, raw, updated_at
                )
                VALUES (
                    %s, %s, %s, %s,
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                    %s, %s, %s, %s, %s, NOW()
                )
                ON CONFLICT (symbol) DO UPDATE
                SET name         = EXCLUDED.name,
                    city         = EXCLUDED.city,
                    country_code = EXCLUDED.country_code,
                    location     = EXCLUDED.location,
                    price        = EXCLUDED.price,
                    prev_close   = EXCLUDED.prev_close,
                    change_pct   = EXCLUDED.change_pct,
                    currency     = EXCLUDED.currency,
                    raw          = EXCLUDED.raw,
                    updated_at   = NOW()
                """,
                (
                    symbol,
                    f"USD/{pin.code} · {pin.name}",
                    pin.city, pin.country_code,
                    pin.lon, pin.lat,
                    latest, previous, change_pct, pin.code,
                    Jsonb(raw_payload),
                ),
            )
            fetched += 1
        conn.commit()

    return {
        "status": "ok",
        "fetched": fetched,
        "skipped": skipped,
        "total": len(CURRENCY_PINS),
    }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    print(ingest_currencies_once())


if __name__ == "__main__":
    main()
