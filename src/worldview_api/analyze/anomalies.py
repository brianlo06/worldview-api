"""Per-country event-rate anomaly detection.

Two passes:
  1. compute_baselines() — rolling 7-day per-country events-per-hour mean + std-dev.
     Skips countries with too little data or too low a baseline (would be noisy).
  2. detect_and_resolve() — for each region with a baseline, compare the last
     hour's rate; fire an anomaly when it spikes past baseline + 3σ AND >= 4
     events AND >= 2× baseline (the three checks together keep false-positives
     down on small populations and noisy regions). Resolve when the rate falls
     back inside baseline + 1σ.

Anomalies carry their driver clusters (top clusters from that region in the
last hour) plus a pulse_lat/lon for the frontend to render a red ring there.
"""

from __future__ import annotations

import logging

from ..db import get_pool
from .synopsis import generate_synopsis

log = logging.getLogger(__name__)


# Floors that keep small-country noise out of the alert pipeline.
MIN_SAMPLE_HOURS = 6
MIN_BASELINE_PER_HOUR = 0.3
TRIGGER_ABS_FLOOR = 4        # current must be >= this many events
TRIGGER_MULT_FACTOR = 2.0    # current must be >= baseline × this
TRIGGER_SIGMA = 3.0          # current must be >= baseline + (this × std-dev)
RESOLVE_SIGMA = 1.0          # rate must fall inside baseline + (this × std-dev)


def compute_baselines() -> dict[str, int]:
    """Recompute per-country baselines from the rolling 7-day window."""
    pool = get_pool()
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            WITH hourly AS (
                SELECT country_code,
                       date_trunc('hour', occurred_at) AS hr,
                       count(*) AS n
                FROM events
                WHERE country_code IS NOT NULL
                  AND occurred_at >= NOW() - INTERVAL '7 days'
                GROUP BY 1, 2
            )
            INSERT INTO region_baselines (
                region_code, baseline_events_per_hr, std_dev, sample_hours, computed_at
            )
            SELECT country_code,
                   avg(n)::real AS baseline,
                   coalesce(stddev_samp(n), 0)::real AS sd,
                   count(*)::int AS sample_hours,
                   NOW()
            FROM hourly
            GROUP BY country_code
            HAVING count(*) >= {MIN_SAMPLE_HOURS}
               AND avg(n) >= {MIN_BASELINE_PER_HOUR}
            ON CONFLICT (region_code) DO UPDATE
            SET baseline_events_per_hr = EXCLUDED.baseline_events_per_hr,
                std_dev                = EXCLUDED.std_dev,
                sample_hours           = EXCLUDED.sample_hours,
                computed_at            = NOW()
            """
        )
        updated = cur.rowcount
        conn.commit()
    log.info("baselines updated: %d regions", updated)
    return {"status": "ok", "updated_regions": updated}


def detect_and_resolve() -> dict[str, int]:
    """Run a single anomaly detection + resolution pass."""
    pool = get_pool()
    triggered = 0
    refreshed = 0
    resolved = 0

    with pool.connection() as conn, conn.cursor() as cur:
        # Per-region recent count + matching baseline
        cur.execute(
            f"""
            WITH recent AS (
                SELECT country_code, count(*)::int AS n
                FROM events
                WHERE occurred_at >= NOW() - INTERVAL '1 hour'
                  AND country_code IS NOT NULL
                GROUP BY 1
            )
            SELECT b.region_code,
                   b.baseline_events_per_hr,
                   b.std_dev,
                   coalesce(r.n, 0) AS current_n
            FROM region_baselines b
            LEFT JOIN recent r ON r.country_code = b.region_code
            WHERE coalesce(r.n, 0) >= {TRIGGER_ABS_FLOOR}
              AND coalesce(r.n, 0) >= {TRIGGER_MULT_FACTOR} * b.baseline_events_per_hr
              AND coalesce(r.n, 0) >= b.baseline_events_per_hr
                                    + {TRIGGER_SIGMA} * GREATEST(b.std_dev, 0.5)
            """
        )
        spikes = cur.fetchall()

        for region, baseline, std_dev, current in spikes:
            denom = max(std_dev or 0.0, 0.5)
            sigma_above = float(current - baseline) / denom

            # Find the top clusters from this region in the last hour — these
            # are the stories driving the spike, surfaced as 'why' on the frontend
            cur.execute(
                """
                SELECT c.id,
                       ST_Y(c.centroid_location::geometry) AS lat,
                       ST_X(c.centroid_location::geometry) AS lon,
                       c.title
                FROM clusters c
                WHERE c.primary_country = %s
                  AND c.last_seen >= NOW() - INTERVAL '1 hour'
                ORDER BY c.event_count DESC, coalesce(c.importance_score, 0) DESC
                LIMIT 5
                """,
                (region,),
            )
            drivers = cur.fetchall()
            driver_ids = [d[0] for d in drivers]
            driver_titles = [d[3] for d in drivers if d[3]]
            multiplier = float(current) / max(float(baseline), 0.1)
            pulse_lat = (
                sum(d[1] for d in drivers if d[1] is not None) / len(drivers)
                if drivers
                else None
            )
            pulse_lon = (
                sum(d[2] for d in drivers if d[2] is not None) / len(drivers)
                if drivers
                else None
            )

            # Touch existing active anomaly, or create a new one
            cur.execute(
                """
                SELECT id, peak_rate, sigma_above, driver_cluster_ids, synopsis
                FROM anomalies
                WHERE region_code = %s AND status = 'active'
                LIMIT 1
                """,
                (region,),
            )
            existing = cur.fetchone()
            if existing:
                ex_id, ex_peak, ex_sigma, ex_driver_ids, ex_synopsis = existing
                # Regenerate the one-line read only when the story set behind
                # the spike actually changed (or it never got one) — refreshes
                # happen every cycle, the situation doesn't.
                synopsis = ex_synopsis
                if ex_synopsis is None or set(ex_driver_ids or []) != set(driver_ids):
                    synopsis = generate_synopsis(region, multiplier, driver_titles)
                cur.execute(
                    """
                    UPDATE anomalies
                    SET peak_rate          = GREATEST(peak_rate, %s),
                        sigma_above        = GREATEST(sigma_above, %s),
                        driver_cluster_ids = %s,
                        pulse_lat          = %s,
                        pulse_lon          = %s,
                        synopsis           = %s,
                        last_seen_at       = NOW()
                    WHERE id = %s
                    """,
                    (current, sigma_above, driver_ids, pulse_lat, pulse_lon,
                     synopsis, ex_id),
                )
                refreshed += 1
            else:
                synopsis = generate_synopsis(region, multiplier, driver_titles)
                cur.execute(
                    """
                    INSERT INTO anomalies (
                        region_code, peak_rate, baseline_rate, sigma_above,
                        driver_cluster_ids, pulse_lat, pulse_lon, synopsis
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (region, current, baseline, sigma_above, driver_ids,
                     pulse_lat, pulse_lon, synopsis),
                )
                triggered += 1
                log.info(
                    "anomaly TRIGGERED %s: %d events vs baseline %.1f (σ=%.1f)",
                    region, current, baseline, sigma_above,
                )

        # Resolve anomalies that have cooled off
        cur.execute(
            f"""
            WITH recent AS (
                SELECT country_code, count(*)::int AS n
                FROM events
                WHERE occurred_at >= NOW() - INTERVAL '1 hour'
                  AND country_code IS NOT NULL
                GROUP BY 1
            )
            UPDATE anomalies a
            SET status   = 'resolved',
                ended_at = NOW()
            FROM region_baselines b
            LEFT JOIN recent r ON r.country_code = b.region_code
            WHERE a.region_code = b.region_code
              AND a.status = 'active'
              AND coalesce(r.n, 0) <= b.baseline_events_per_hr
                                    + {RESOLVE_SIGMA} * GREATEST(b.std_dev, 0.5)
            RETURNING a.region_code
            """
        )
        resolved = cur.rowcount
        conn.commit()

    log.info(
        "anomalies: %d new, %d refreshed, %d resolved",
        triggered, refreshed, resolved,
    )
    return {
        "status": "ok",
        "triggered": triggered,
        "refreshed": refreshed,
        "resolved": resolved,
    }


def run_anomalies_once() -> dict[str, int]:
    compute_baselines()
    return detect_and_resolve()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    print(run_anomalies_once())


if __name__ == "__main__":
    main()
