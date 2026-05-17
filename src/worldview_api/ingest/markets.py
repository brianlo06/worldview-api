"""Stooq-backed market quotes for major global indices and ETFs.

Stooq provides free, no-API-key CSV quotes. We pin each symbol to a
financial center on the globe; the frontend colors dots green/red
based on change_pct.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from psycopg.types.json import Jsonb

from ..db import get_pool

log = logging.getLogger(__name__)

STOOQ_QUOTE_URL = (
    "https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcvn&h&e=csv"
)


@dataclass(frozen=True)
class Instrument:
    symbol: str        # Stooq symbol (e.g., "^spx", "spy.us")
    name: str
    city: str
    country_code: str
    lat: float
    lon: float
    currency: str = "USD"


# Curated set anchored to financial centers. Diversified across continents.
INSTRUMENTS: tuple[Instrument, ...] = (
    # Americas
    Instrument("^spx",  "S&P 500",         "New York",      "US", 40.71, -74.00, "USD"),
    Instrument("^ndq",  "Nasdaq Composite","New York",      "US", 40.74, -73.99, "USD"),
    Instrument("^dji",  "Dow Jones",       "New York",      "US", 40.70, -74.01, "USD"),
    Instrument("spy.us","SPDR S&P 500 ETF","New York",      "US", 40.72, -74.00, "USD"),
    Instrument("^bvp",  "Bovespa",         "São Paulo",     "BR", -23.55, -46.63, "BRL"),
    # Europe
    Instrument("^ukx",  "FTSE 100",        "London",        "GB", 51.51,  -0.13, "GBP"),
    Instrument("^dax",  "DAX 40",          "Frankfurt",     "DE", 50.11,   8.68, "EUR"),
    Instrument("^cac",  "CAC 40",          "Paris",         "FR", 48.86,   2.35, "EUR"),
    Instrument("^smi",  "Swiss Market",    "Zurich",        "CH", 47.37,   8.55, "CHF"),
    # Asia / Pacific
    Instrument("^nkx",  "Nikkei 225",      "Tokyo",         "JP", 35.68, 139.69, "JPY"),
    Instrument("^hsi",  "Hang Seng",       "Hong Kong",     "HK", 22.30, 114.18, "HKD"),
    Instrument("^shc",  "Shanghai Comp.",  "Shanghai",      "CN", 31.23, 121.47, "CNY"),
    Instrument("^kospi","KOSPI",           "Seoul",         "KR", 37.57, 126.98, "KRW"),
    Instrument("^sti",  "Straits Times",   "Singapore",     "SG",  1.35, 103.82, "SGD"),
    Instrument("^bse",  "Sensex",          "Mumbai",        "IN", 19.08,  72.88, "INR"),
    Instrument("^aord", "All Ordinaries",  "Sydney",        "AU", -33.87, 151.21, "AUD"),
)

HEADERS = {
    "User-Agent": "worldview-dev/0.1",
    "Accept": "text/csv",
}


def _parse_quote_csv(text: str) -> dict[str, str] | None:
    """Stooq returns a header row followed by one data row.

    Example:
        Symbol,Date,Time,Open,High,Low,Close,Volume,Name
        SPY.US,2026-05-13,21:30:00,580.21,583.40,579.45,582.10,75234567,SPDR S&P 500
    """
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if len(rows) < 2:
        return None
    header = [h.strip() for h in rows[0]]
    values = rows[1]
    if "N/D" in values:
        return None
    record = dict(zip(header, values))
    return record


def _to_float(s: str | None) -> float | None:
    if s is None or s == "" or s == "N/D":
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def fetch_quote(client: httpx.Client, inst: Instrument) -> dict[str, object] | None:
    url = STOOQ_QUOTE_URL.format(symbol=inst.symbol)
    try:
        r = client.get(url, timeout=15.0)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning("market fetch failed for %s: %s", inst.symbol, e)
        return None
    record = _parse_quote_csv(r.text)
    if not record:
        log.warning("market fetch returned no data for %s", inst.symbol)
        return None

    close = _to_float(record.get("Close"))
    open_ = _to_float(record.get("Open"))
    if close is None or open_ is None or open_ == 0:
        log.warning("market fetch incomplete for %s: %s", inst.symbol, record)
        return None

    change_pct = (close - open_) / open_ * 100.0
    return {
        "price": close,
        "prev_close": open_,
        "change_pct": change_pct,
        "raw": record,
    }


def ingest_markets_once() -> dict[str, int | str]:
    pool = get_pool()
    fetched = 0
    skipped = 0

    with httpx.Client(headers=HEADERS) as client:
        with pool.connection() as conn, conn.cursor() as cur:
            for inst in INSTRUMENTS:
                quote = fetch_quote(client, inst)
                if quote is None:
                    skipped += 1
                    continue
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
                        inst.symbol, inst.name, inst.city, inst.country_code,
                        inst.lon, inst.lat,
                        quote["price"], quote["prev_close"], quote["change_pct"],
                        inst.currency, Jsonb(quote["raw"]),
                    ),
                )
                fetched += 1
            conn.commit()

    return {
        "status": "ok",
        "fetched": fetched,
        "skipped": skipped,
        "total": len(INSTRUMENTS),
    }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    print(ingest_markets_once())


if __name__ == "__main__":
    main()
