"""GDELT GKG (Global Knowledge Graph) 2.1 ingestion.

GKG is a strict upgrade over the events file for our needs:
  - Includes the real article title (parsed from V2EXTRASXML)
  - Includes og:image URL (V2_1SHAREIMG)
  - Includes multiple geocoded locations per article (V2ENHANCEDLOCATIONS)
  - Includes theme codes that map directly onto our category typology
  - Less US-skewed than the events file

Same 15-min publishing cadence as events; we pull the third URL from
lastupdate.txt (events / mentions / gkg).
"""

from __future__ import annotations

import csv
import html
import io
import logging
import re
import zipfile
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from psycopg.types.json import Jsonb

from ..db import get_pool
from ..titles import is_generic_title
from .common import GDELT_LASTUPDATE_URL, url_hash
from .common import http_headers as _http_headers
from .common import parse_gdelt_timestamp as parse_gkg_date

log = logging.getLogger(__name__)

# GKG 2.1 column order, in the order they appear in the .csv file.
GKG_COLUMNS: tuple[str, ...] = (
    "GKGRECORDID",
    "V21DATE",
    "V2SOURCECOLLECTIONIDENTIFIER",
    "V2SOURCECOMMONNAME",
    "V2DOCUMENTIDENTIFIER",
    "V1COUNTS",
    "V21COUNTS",
    "V1THEMES",
    "V2ENHANCEDTHEMES",
    "V1LOCATIONS",
    "V2ENHANCEDLOCATIONS",
    "V1PERSONS",
    "V2ENHANCEDPERSONS",
    "V1ORGANIZATIONS",
    "V2ENHANCEDORGANIZATIONS",
    "V15TONE",
    "V21ENHANCEDDATES",
    "V2GCAM",
    "V21SHAREIMG",
    "V21RELATEDIMAGES",
    "V21SOCIALIMAGEEMBEDS",
    "V21SOCIALVIDEOEMBEDS",
    "V21QUOTATIONS",
    "V21ALLNAMES",
    "V21AMOUNTS",
    "V21TRANSLATIONINFO",
    "V2EXTRASXML",
)

# Theme-to-category map. `themes_to_category` counts regex hits across all
# patterns and picks the category with the most matches; ties fall back to
# the declaration order below (earlier categories win).
# Note: do NOT add `TAX_` to any pattern — it's GDELT's taxonomy *prefix*
# (TAX_FNCACT, TAX_ETHNICITY, TAX_WORLDLANGUAGES, ...), not a content signal.
# ECON_* already covers genuine taxation themes (ECON_TAXATION, etc.).
# Similarly, the politics pattern does NOT include bare `GOVERNMENT` — that
# substring matches noise tokens like GENERAL_GOVERNMENT and WB_*_GOVERNMENT.
# GDELT's actual government namespace is `GOV_*`, which is what we match.
# Reference: http://data.gdeltproject.org/documentation/GKG-CATEGORY-TAXONOMY.txt
_THEME_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(KILL|ATTACK|TERROR|MIL_|ARMEDCONFLICT|MILITARY_OPS|ETHNIC_CLEANSING|WMD|INSURGENC)", re.I), "conflict"),
    (re.compile(r"(EARTHQUAKE|VOLCANIC|VOLCANO|TSUNAMI|SEISMIC)", re.I), "quake"),
    (re.compile(r"(FLOOD|HURRICANE|TORNADO|CYCLONE|STORM|DROUGHT|WILDFIRE|BLIZZARD|TYPHOON|WEATHER_)", re.I), "weather"),
    (re.compile(r"(PROTEST|RIOT|STRIKE|DEMONSTRATION|ACTIVISM|CIVIL_UNREST)", re.I), "social"),
    (re.compile(r"(ECON_|TRADE|FINANCE|BUSINESS|MARKETS|INFLATION|UNEMPLOYMENT|CORRUPTION)", re.I), "business"),
    (re.compile(r"(ELECTION|GOV_|POLITICAL|DIPLOM|REBEL|REGIME|SANCTION|TREATY)", re.I), "politics"),
)

# GDELT attaches auxiliary taxonomy themes to articles independently of
# article topic. They're useful for cross-referencing in research but they
# inflate category counts when matched by substring. Tokens with these
# prefixes are dropped before regex matching in `themes_to_category`.
#   EPU_  — Economic Policy Uncertainty (Baker, Bloom, Davis 2016 dataset).
#           e.g. EPU_POLICY_GOVERNMENT is about economic uncertainty, not
#           government action.
#   WB_   — World Bank topic taxonomy (thousands of codes). e.g.
#           WB_678_DIGITAL_GOVERNMENT gets sprayed onto digital-economy
#           articles regardless of topic.
_NOISE_PREFIXES: tuple[str, ...] = ("EPU_", "WB_")

# Specific known-noise tokens that don't have a useful prefix to filter on.
# GENERAL_GOVERNMENT is a generic descriptor GDELT applies to most articles
# that mention government in any context.
_NOISE_TOKENS: frozenset[str] = frozenset({"GENERAL_GOVERNMENT"})
_DEFAULT_CATEGORY = "politics"

# Type weights for the location scorer. Each mention of a location contributes
# `weight × 1.0` to that location's score; the location with the highest total
# wins. City-level locations are weighted highest because the article being
# *about* a specific city is the most useful signal; countries are weighted
# low because articles that drag in a country name in passing shouldn't pull
# the dot to the country centroid when a city in the same article was the
# real subject.
_TYPE_WEIGHTS: dict[int, float] = {
    3: 1.0,   # US City
    4: 1.0,   # World City
    2: 0.7,   # US State
    5: 0.7,   # World State
    1: 0.3,   # Country
}

# Map GDELT location type → geo_precision label written to the DB.
_TYPE_TO_PRECISION: dict[int, str] = {
    3: "city",
    4: "city",
    2: "state",
    5: "state",
    1: "country",
}

# Tone score → importance bump
_TONE_EXTREME_THRESHOLD = 5.0

# importance_from_row constants. The 0..1 score combines four bounded signals
# (tone extremity, theme richness, location richness, entity richness) on top
# of a low base, so the *typical* GKG article lands mid-scale and only
# multi-signal-rich content approaches 1.0. Retune by editing here.
IMPORTANCE_BASE = 0.15
# Denominators are deliberately large so the *typical* GKG article (which is
# theme-heavy, location-heavy, and entity-heavy by GDELT's nature) doesn't
# saturate any single term. Saturation should signal "this is an outlier",
# not "this is the median."
TONE_CAP, TONE_DIV = 0.25, 40.0
THEME_CAP, THEME_DIV = 0.20, 120.0
LOC_CAP, LOC_DIV = 0.15, 40.0
# GKG has no per-article mention count in its CSV; entity richness
# (V2ENHANCEDPERSONS + V2ENHANCEDORGANIZATIONS) serves the same role —
# substantive news pieces name many people and organizations.
ENTITY_CAP, ENTITY_DIV = 0.15, 200.0


def fetch_latest_gkg_url() -> str:
    """Return the URL of the latest GKG csv.zip (line 3 of lastupdate.txt)."""
    resp = httpx.get(GDELT_LASTUPDATE_URL, timeout=20, headers=_http_headers())
    resp.raise_for_status()
    lines = resp.text.strip().splitlines()
    if len(lines) < 3:
        raise RuntimeError(f"Unexpected lastupdate.txt: {resp.text!r}")
    # Line index 2 is GKG (0=events, 1=mentions, 2=gkg)
    parts = lines[2].split()
    if len(parts) < 3:
        raise RuntimeError(f"Bad GKG line: {lines[2]!r}")
    return parts[2]


def download_gkg_csv(url: str) -> list[dict[str, str]]:
    """Download + unzip + parse a GKG csv.zip into a list of column-dicts."""
    resp = httpx.get(url, timeout=120, headers=_http_headers())
    resp.raise_for_status()
    rows: list[dict[str, str]] = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as f:
            # latin-1 because GKG sometimes has non-UTF-8 bytes in titles
            text = io.TextIOWrapper(f, encoding="latin-1", errors="replace")
            reader = csv.reader(text, delimiter="\t", quoting=csv.QUOTE_NONE)
            for raw in reader:
                if len(raw) < len(GKG_COLUMNS):
                    raw = raw + [""] * (len(GKG_COLUMNS) - len(raw))
                rows.append(dict(zip(GKG_COLUMNS, raw)))
    return rows


_TITLE_RE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.IGNORECASE | re.DOTALL)
_NUL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def extract_title(extras_xml: str | None) -> str | None:
    if not extras_xml:
        return None
    m = _TITLE_RE.search(extras_xml)
    if not m:
        return None
    title = html.unescape(m.group(1)).strip()
    title = _NUL_RE.sub("", title)
    title = re.sub(r"\s+", " ", title)
    if len(title) > 240:
        title = title[:240] + "…"
    return title or None


ParsedLoc = tuple[int, str, str | None, float, float, int | None]
#  ^type  ^name  ^cc            ^lat   ^lon   ^char-offset (None when absent)


def parse_locations(loc_str: str | None) -> list[ParsedLoc]:
    """Parse V2ENHANCEDLOCATIONS into one tuple per *mention*.

    Format per location is type-first, not offset-first as the V2 docs imply:
        `<type>#<name>#<cc>#<adm1>#<adm2>#<lat>#<lon>#<featureid>[#<offset>]`

    A single article often mentions the same location multiple times — each
    mention becomes one tuple in the output, so the caller can count them.
    """
    if not loc_str:
        return []
    out: list[ParsedLoc] = []
    for part in loc_str.split(";"):
        if not part.strip():
            continue
        fields = part.split("#")
        # Minimum: type, name, cc, adm1, adm2, lat, lon, featureid → 8 fields
        if len(fields) < 7:
            continue
        try:
            loc_type = int(fields[0])
            name = fields[1] or ""
            cc = (fields[2] or "").strip() or None
            lat = float(fields[5])
            lon = float(fields[6])
        except (ValueError, IndexError):
            continue
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            continue
        if lat == 0.0 and lon == 0.0:
            # GDELT uses (0,0) as a placeholder for missing geo
            continue
        offset: int | None = None
        if len(fields) >= 9:
            try:
                offset = int(fields[8])
            except (ValueError, IndexError):
                offset = None
        out.append((loc_type, name, cc, lat, lon, offset))
    return out


def score_locations(
    locs: list[ParsedLoc],
) -> list[tuple[float, ParsedLoc]]:
    """Score each *distinct* location by `mention_count × type_weight`.

    Locations are considered the same when they share `(name, lat, lon)`
    rounded to 2 decimals — that's tight enough to separate Beijing from
    Shanghai and loose enough to merge minor GDELT geocoding wiggle.

    Returned list is sorted descending by score, with character-offset of
    the earliest mention as the tiebreaker (earlier in the article = more
    likely to be the dateline / lead-paragraph subject).
    """
    if not locs:
        return []

    groups: dict[tuple[str, float, float], list[ParsedLoc]] = {}
    for loc in locs:
        _t, name, _cc, lat, lon, _off = loc
        key = (name.lower(), round(lat, 2), round(lon, 2))
        groups.setdefault(key, []).append(loc)

    scored: list[tuple[float, int, ParsedLoc]] = []
    for group in groups.values():
        weight = _TYPE_WEIGHTS.get(group[0][0], 0.5)
        score = len(group) * weight
        # Earliest character offset across all mentions of this location.
        # None offsets sort last (treated as "very late").
        offsets = [g[5] for g in group if g[5] is not None]
        earliest = min(offsets) if offsets else 10**9
        # Represent the group by the mention with the earliest offset, so
        # downstream uses the most "lead-paragraph" coordinate.
        canonical = min(group, key=lambda g: g[5] if g[5] is not None else 10**9)
        scored.append((score, earliest, canonical))

    # Highest score first; earliest offset breaks ties.
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [(score, loc) for score, _off, loc in scored]


def pick_best_location(locs: list[ParsedLoc]) -> ParsedLoc | None:
    """Return the location with the highest mention-weighted score, or None."""
    scored = score_locations(locs)
    return scored[0][1] if scored else None


def type_to_precision(loc_type: int) -> str:
    """Map a GDELT location type code to the geo_precision label."""
    return _TYPE_TO_PRECISION.get(loc_type, "country")


def themes_to_category(themes_str: str | None) -> str:
    """Pick the dominant category by counting theme hits per category.

    Tokenizes the GKG theme string by `;` and strips each token's `,offset`
    suffix, then drops tokens that belong to known auxiliary GDELT taxonomies
    (see `_NOISE_PREFIXES`, `_NOISE_TOKENS`) before applying the per-category
    regexes. Counts matches per category; ties fall back to the declaration
    order in `_THEME_PATTERNS`.

    First-match-wins picked the wrong category when a story had one tangential
    disaster theme (e.g. `NATURAL_DISASTER_EARTHQUAKE` on a trade article about
    Türkiye) alongside many themes from another category. Counting matches and
    taking the majority avoids that.
    """
    if not themes_str:
        return _DEFAULT_CATEGORY

    filtered: list[str] = []
    for raw in themes_str.split(";"):
        tok = raw.split(",", 1)[0].strip()
        if not tok:
            continue
        if tok in _NOISE_TOKENS:
            continue
        if any(tok.startswith(p) for p in _NOISE_PREFIXES):
            continue
        filtered.append(tok)
    if not filtered:
        return _DEFAULT_CATEGORY
    filtered_str = ";".join(filtered)

    counts: dict[str, int] = {}
    for pattern, category in _THEME_PATTERNS:
        hits = len(pattern.findall(filtered_str))
        if hits:
            counts[category] = counts.get(category, 0) + hits
    if not counts:
        return _DEFAULT_CATEGORY
    priority = {cat: i for i, (_, cat) in enumerate(_THEME_PATTERNS)}
    return max(counts.items(), key=lambda kv: (kv[1], -priority.get(kv[0], 99)))[0]


def _to_float(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def importance_from_row(row: dict[str, str], theme_count: int, loc_count: int) -> float:
    """Heuristic 0..1 importance for a GKG document.

    Combines four bounded signals on a low base so typical content lands
    mid-scale and only multi-signal-rich articles approach 1.0:
      - tone extremity (|avg_tone| from V15TONE)
      - theme richness (theme_count)
      - location richness (loc_count)
      - entity richness (persons + organizations counted from V2ENHANCED* fields)
    Constants live at module top to keep retuning a one-place edit.
    """
    base = IMPORTANCE_BASE

    # V15TONE: comma-separated [avg_tone, positive, negative, polarity, ...]
    tone_str = row.get("V15TONE") or ""
    parts = tone_str.split(",")
    if parts:
        avg_tone = _to_float(parts[0]) or 0.0
        base += min(TONE_CAP, abs(avg_tone) / TONE_DIV)

    base += min(THEME_CAP, theme_count / THEME_DIV)
    base += min(LOC_CAP, loc_count / LOC_DIV)

    persons = row.get("V2ENHANCEDPERSONS") or ""
    orgs = row.get("V2ENHANCEDORGANIZATIONS") or ""
    entity_count = sum(
        1 for s in (persons + ";" + orgs).split(";") if s.strip()
    )
    base += min(ENTITY_CAP, entity_count / ENTITY_DIV)

    return max(0.0, min(1.0, base))


def _is_brand_only_title(title: str, outlet_host: str) -> bool:
    """True when the scraped title is just the site's brand, not a headline.

    GDELT sometimes scrapes pages like /print-article/12345/ that only put the
    site name in <title>, producing "Deadline" from deadline.com. These have no
    informational content and should not become events.
    """
    t = title.strip().lower()
    if not t or len(t.split()) >= 3:
        return False
    brand = outlet_host.split(".")[0].lower() if outlet_host else ""
    if not brand:
        return False
    # Strip non-alphanumerics for the comparison ("BBC News" -> "bbcnews")
    t_norm = re.sub(r"[^a-z0-9]+", "", t)
    return brand in t_norm and len(t_norm) <= len(brand) + 4


def _is_junk_title(title: str, outlet_host: str) -> bool:
    """Brand-only titles plus bare section names ("World", "Top Stories").
    The section-name half lives in worldview_api.titles so the
    representative picker and breaking gate apply the same test to events
    ingested before this gate existed."""
    if _is_brand_only_title(title, outlet_host):
        return True
    return is_generic_title(title)


def _clean_loc_short(loc_name: str | None, geo_precision: str) -> str | None:
    """Normalize the location name into the `city` column value.

    Drops the ", State, Country" suffix (we store country separately), strips
    stray leading punctuation that GDELT sometimes emits (e.g. "-Queens",
    "-River Falls"), and returns None for country-precision locations or when
    what remains doesn't start with a letter (would be junk like a digit or
    a stray symbol). One-or-two-letter remainders are FIPS/ADM codes, not
    city names ("SF" = South Africa), and would mislead anything that reads
    the field as a place name — dropped.
    """
    if not loc_name or geo_precision == "country":
        return None
    short = loc_name.split(",")[0].strip().lstrip("-., ").strip()
    if len(short) <= 2 or not short[0].isalpha():
        return None
    return short


def humanize_outlet(url: str, fallback: str | None) -> str:
    if fallback:
        return fallback.lower()
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    return host[4:] if host.startswith("www.") else host


def ingest_gkg_once() -> dict[str, Any]:
    url = fetch_latest_gkg_url()
    log.info("gkg latest: %s", url)

    pool = get_pool()

    # Watermark check — same source string as events, but cursor records "gkg:URL"
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT cursor FROM source_watermarks WHERE source = 'gdelt_gkg'"
        )
        row = cur.fetchone()
    if row and row[0] == url:
        return {"status": "already_processed", "url": url, "rows": 0}

    rows = download_gkg_csv(url)
    log.info("gkg parsed %d rows", len(rows))

    inserted_raw = 0
    inserted_events = 0
    skipped = 0

    with pool.connection() as conn:
        with conn.cursor() as cur:
            for r in rows:
                src_url = (r.get("V2DOCUMENTIDENTIFIER") or "").strip()
                if not src_url:
                    skipped += 1
                    continue

                title = extract_title(r.get("V2EXTRASXML"))
                if not title:
                    skipped += 1
                    continue

                outlet_host = humanize_outlet(src_url, r.get("V2SOURCECOMMONNAME"))
                if _is_junk_title(title, outlet_host):
                    skipped += 1
                    continue

                locs = parse_locations(r.get("V2ENHANCEDLOCATIONS"))
                best = pick_best_location(locs)
                if best is None:
                    skipped += 1
                    continue
                loc_type, loc_name, loc_cc, lat, lon, _offset = best
                geo_precision = type_to_precision(loc_type)
                loc_short = _clean_loc_short(loc_name, geo_precision)

                themes = r.get("V2ENHANCEDTHEMES") or r.get("V1THEMES") or ""
                theme_count = len([t for t in themes.split(";") if t.strip()])
                category = themes_to_category(themes)

                image_url = (r.get("V21SHAREIMG") or "").strip() or None
                outlet = outlet_host
                occurred_at = parse_gkg_date(r.get("V21DATE"))
                gkg_id = (r.get("GKGRECORDID") or "").strip()
                source_id = gkg_id or url_hash(src_url)
                u_hash = url_hash(src_url)
                importance = importance_from_row(r, theme_count, len(locs))
                country = loc_cc[:2] if loc_cc and len(loc_cc) >= 2 else None
                cats = [category]

                try:
                    cur.execute(
                        """
                        INSERT INTO raw_events (source, source_id, payload)
                        VALUES ('gdelt_gkg', %s, %s)
                        ON CONFLICT (source, source_id) DO NOTHING
                        RETURNING id
                        """,
                        (source_id, Jsonb(r)),
                    )
                    raw_row = cur.fetchone()
                    if raw_row is None:
                        skipped += 1
                        continue
                    inserted_raw += 1
                    raw_id = raw_row[0]

                    cur.execute(
                        """
                        INSERT INTO events (
                            raw_event_id, title, url, url_hash,
                            source, source_outlet, occurred_at,
                            location, country_code, city, categories,
                            importance, image_url, geo_precision, raw,
                            scraped_at, scrape_status
                        )
                        VALUES (
                            %s, %s, %s, %s,
                            'gdelt_gkg', %s, %s,
                            ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                            %s, %s, %s,
                            %s, %s, %s, %s,
                            NOW(), 'ok'
                        )
                        ON CONFLICT (url_hash) DO NOTHING
                        """,
                        (
                            raw_id, title, src_url, u_hash,
                            outlet, occurred_at,
                            lon, lat,
                            country, loc_short, cats,
                            importance, image_url, geo_precision, Jsonb(r),
                        ),
                    )
                    if cur.rowcount > 0:
                        inserted_events += 1

                    cur.execute(
                        "UPDATE raw_events SET processed_at = NOW() WHERE id = %s",
                        (raw_id,),
                    )
                except Exception as e:  # noqa: BLE001
                    log.warning("gkg row error for %s: %s", source_id, e)
                    skipped += 1

            cur.execute(
                """
                INSERT INTO source_watermarks (source, last_seen_at, cursor)
                VALUES ('gdelt_gkg', %s, %s)
                ON CONFLICT (source) DO UPDATE
                SET last_seen_at = EXCLUDED.last_seen_at,
                    cursor       = EXCLUDED.cursor,
                    updated_at   = NOW()
                """,
                (datetime.now(timezone.utc), url),
            )
        conn.commit()

    return {
        "status": "ok",
        "url": url,
        "parsed": len(rows),
        "inserted_raw": inserted_raw,
        "inserted_events": inserted_events,
        "skipped": skipped,
    }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    print(ingest_gkg_once())


if __name__ == "__main__":
    main()
