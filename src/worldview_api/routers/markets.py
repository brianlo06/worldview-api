"""Market quotes endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from ..db import get_pool
from ..schemas import MarketOut

router = APIRouter()


@router.get("/markets", response_model=list[MarketOut])
def markets() -> list[MarketOut]:
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol, name, city, country_code,
                   ST_Y(location::geometry) AS lat,
                   ST_X(location::geometry) AS lon,
                   price, prev_close, change_pct, currency, updated_at
            FROM markets
            ORDER BY change_pct DESC NULLS LAST
            """
        )
        rows = cur.fetchall()
    return [
        MarketOut(
            symbol=r[0],
            name=r[1],
            city=r[2],
            country_code=r[3],
            lat=r[4],
            lon=r[5],
            price=float(r[6]) if r[6] is not None else None,
            prev_close=float(r[7]) if r[7] is not None else None,
            change_pct=r[8],
            currency=r[9],
            updated_at=r[10],
        )
        for r in rows
    ]
