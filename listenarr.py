import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

TIMEOUT = (5, 30)
MBID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12}$"
)

ALLOWED_RANGES = {
    "this_week",
    "this_month",
    "this_year",
    "week",
    "month",
    "quarter",
    "year",
    "half_yearly",
    "all_time",
}
ALLOWED_SOURCES = {"koito", "listenbrainz"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("listenarr")
logging.getLogger("urllib3").setLevel(logging.ERROR)


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_int_env(name: str, default: int) -> int:
    value = os.getenv(name, str(default)).strip()
    try:
        result = int(value)
    except ValueError as exc:
        raise RuntimeError(
            f"Environment variable {name} must be an integer, got: {value}"
        ) from exc
    if result < 1:
        raise RuntimeError(f"Environment variable {name} must be greater than 0")
    return result


def get_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise RuntimeError(
        f"Environment variable {name} must be a boolean, got: {value}"
    )


def get_source_env() -> str:
    value = os.getenv("SOURCE", "").strip().lower()
    if value not in ALLOWED_SOURCES:
        raise RuntimeError(
            f"Environment variable SOURCE must be one of "
            f"{sorted(ALLOWED_SOURCES)}, got: {value}"
        )
    return value


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 504],
        allowed_methods=["GET", "POST"],
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def lidarr_headers(api_key: str) -> dict[str, str]:
    return {
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def token_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def get_excluded_artists(
    session: requests.Session,
    lidarr_url: str,
    api_key: str,
) -> set[str]:
    response = session.get(
        f"{lidarr_url.rstrip('/')}/api/v1/importlistexclusion",
        headers=lidarr_headers(api_key),
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected Lidarr exclusion response: {data}")
    return {
        str(item["foreignId"])
        for item in data
        if isinstance(item, dict) and item.get("foreignId")
    }


def get_existing_artists(
    session: requests.Session,
    lidarr_url: str,
    api_key: str,
) -> set[str]:
    response = session.get(
        f"{lidarr_url.rstrip('/')}/api/v1/artist",
        headers=lidarr_headers(api_key),
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected Lidarr artist response: {data}")
    return {
        str(item["foreignArtistId"])
        for item in data
        if isinstance(item, dict) and item.get("foreignArtistId")
    }


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        parsed = parse_iso_datetime(value)
        if parsed:
            return parsed
        try:
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def get_cutoff(time_range: str) -> datetime | None:
    now = datetime.now(timezone.utc)
    if time_range in {"week", "this_week"}:
        return now - timedelta(days=7)
    if time_range in {"month", "this_month"}:
        return now - timedelta(days=30)
    if time_range == "quarter":
        return now - timedelta(days=90)
    if time_range == "half_yearly":
        return now - timedelta(days=182)
    if time_range in {"year", "this_year"}:
        return now - timedelta(days=365)
    if time_range == "all_time":
        return None
    raise ValueError(
        f"Invalid TIME_RANGE: {time_range}. "
        f"Allowed: {sorted(ALLOWED_RANGES)}"
    )


def is_valid_mbid(value: Any) -> bool:
    return isinstance(value, str) and bool(MBID_RE.fullmatch(value))


def lookup_artist_mbid(
    session: requests.Session,
    artist_name: str,
    cache: dict[str, str | None],
) -> str | None:
    normalized_name = artist_name.strip().casefold()
    if normalized_name in cache:
        return cache[normalized_name]

    for attempt in range(1, 4):
        try:
            response = session.get(
                "https://musicbrainz.org/ws/2/artist",
                params={
                    "query": f'artist:"{artist_name}"',
                    "fmt": "json",
                    "limit": 10,
                },
                headers={
                    "User-Agent": (
                        "listenarr/1.0 "
                        "(personal music automation)"
                    ),
                    "Accept": "application/json",
                },
                timeout=TIMEOUT,
            )

            if response.status_code == 503:
                if attempt < 3:
                    logger.warning(
                        "MusicBrainz returned 503 for %s; "
                        "retrying in 60 seconds (%d/3)",
                        artist_name,
                        attempt + 1,
                    )
                    time.sleep(60)
                    continue
                cache[normalized_name] = None
                return None

            response.raise_for_status()
            results = response.json().get("artists", [])
            break

        except requests.Timeout as exc:
            if attempt < 3:
                logger.warning(
                    "MusicBrainz timeout for %s; "
                    "retrying in 60 seconds (%d/3): %s",
                    artist_name,
                    attempt + 1,
                    exc,
                )
                time.sleep(60)
                continue
            cache[normalized_name] = None
            return None

        except requests.RequestException as exc:
            if attempt < 3:
                logger.warning(
                    "MusicBrainz request failed for %s; "
                    "retrying in 60 seconds (%d/3): %s",
                    artist_name,
                    attempt + 1,
                    exc,
                )
                time.sleep(60)
                continue
            cache[normalized_name] = None
            return None
    else:
        cache[normalized_name] = None
        return None

    normalized_results = [
        artist
        for artist in results
        if isinstance(artist, dict)
    ]

    exact_matches = [
        artist
        for artist in normalized_results
        if artist.get("name", "").casefold() == normalized_name
    ]

    candidates = exact_matches or normalized_results
    mbid = candidates[0].get("id") if candidates else None

    if not is_valid_mbid(mbid):
        cache[normalized_name] = None
        return None

    cache[normalized_name] = mbid
    time.sleep(1.1)
    return mbid


def get_koito_artist_mbid(
    session: requests.Session,
    koito_url: str,
    koito_token: str,
    koito_artist_id: int,
    cache: dict[int, str | None],
) -> str | None:
    if koito_artist_id in cache:
        return cache[koito_artist_id]

    url = (
        f"{koito_url.rstrip('/')}/apis/web/v1/artist/"
        f"{koito_artist_id}"
    )

    try:
        response = session.get(
            url,
            headers=token_headers(koito_token),
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        logger.warning(
            "Koito artist lookup failed for ID %s: %s",
            koito_artist_id,
            exc,
        )
        cache[koito_artist_id] = None
        return None

    mbid = (
        data.get("musicbrainz_id")
        or data.get("musicbrainzId")
        or data.get("mbid")
    )

    if not is_valid_mbid(mbid):
        cache[koito_artist_id] = None
        return None

    cache[koito_artist_id] = mbid
    return mbid


def extract_artist_info(
    listen: dict[str, Any],
) -> tuple[str | None, str | None, datetime | None, int | None]:
    listened_at = parse_timestamp(listen.get("time"))
    artist_name: str | None = None
    artist_mbid: str | None = None
    koito_artist_id: int | None = None

    track = listen.get("track")

    if isinstance(track, dict):
        artists = track.get("artists")

        if isinstance(artists, list) and artists:
            first_artist = artists[0]

            if isinstance(first_artist, dict):
                artist_name = (
                    first_artist.get("name")
                    or first_artist.get("artist_name")
                    or first_artist.get("artistName")
                )
                artist_mbid = (
                    first_artist.get("musicbrainz_id")
                    or first_artist.get("musicbrainzId")
                    or first_artist.get("mbid")
                    or first_artist.get("artist_mbid")
                    or first_artist.get("artistMbid")
                )
                koito_artist_id = first_artist.get("id")

        artist_name = (
            artist_name
            or track.get("artist_name")
            or track.get("artistName")
        )
        artist_mbid = (
            artist_mbid
            or track.get("musicbrainz_id")
            or track.get("musicbrainzId")
            or track.get("artist_mbid")
            or track.get("artistMbid")
        )

    artist_name = (
        artist_name
        or listen.get("artist_name")
        or listen.get("artistName")
    )
    artist_mbid = (
        artist_mbid
        or listen.get("musicbrainz_id")
        or listen.get("musicbrainzId")
        or listen.get("artist_mbid")
        or listen.get("artistMbid")
    )

    try:
        koito_artist_id = (
            int(koito_artist_id)
            if koito_artist_id is not None
            else None
        )
    except (TypeError, ValueError):
        koito_artist_id = None

    return (
        str(artist_name) if artist_name else None,
        str(artist_mbid) if artist_mbid else None,
        listened_at,
        koito_artist_id,
    )


def extract_listens(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [item for item in data["items"] if isinstance(item, dict)]

    raise RuntimeError(f"Could not find listens list in Koito response: {data}")


def fetch_recent_koito_listens(
    session: requests.Session,
    koito_url: str,
    koito_token: str,
    username: str,
    count: int,
    time_range: str,
) -> list[dict[str, Any]]:
    del username

    logger.info("Fetching listens from Koito")
    url = f"{koito_url.rstrip('/')}/apis/web/v1/listens"
    cutoff = get_cutoff(time_range)
    target_count = max(count * 10, 200)
    page = 1
    listens: list[dict[str, Any]] = []

    while len(listens) < target_count:
        response = session.get(
            url,
            headers=token_headers(koito_token),
            params={
                "page": page,
                "period": time_range,
            },
            timeout=TIMEOUT,
        )
        response.raise_for_status()

        body = response.json()
        page_items = extract_listens(body)
        listens.extend(page_items)

        if not page_items or not body.get("has_next_page", False):
            break

        page += 1

    if cutoff is None:
        return listens

    filtered: list[dict[str, Any]] = []

    for listen in listens:
        listened_at = extract_artist_info(listen)[2]
        if listened_at is None or listened_at >= cutoff:
            filtered.append(listen)

    return filtered


def get_top_artists_from_koito(
    session: requests.Session,
    koito_url: str,
    koito_token: str,
    username: str,
    time_range: str,
    count: int,
    min_listen: int,
) -> list[dict[str, Any]]:
    listens = fetch_recent_koito_listens(
        session=session,
        koito_url=koito_url,
        koito_token=koito_token,
        username=username,
        count=count,
        time_range=time_range,
    )

    name_counter: Counter[str] = Counter()
    names_display: dict[str, str] = {}
    koito_ids: dict[str, int] = {}
    inline_mbids: dict[str, str] = {}

    for listen in listens:
        artist_name, artist_mbid, _, koito_artist_id = extract_artist_info(
            listen
        )

        if not artist_name:
            continue

        normalized_name = artist_name.casefold()
        name_counter[normalized_name] += 1
        names_display[normalized_name] = artist_name

        if koito_artist_id is not None:
            koito_ids[normalized_name] = koito_artist_id

        if is_valid_mbid(artist_mbid):
            inline_mbids[normalized_name] = artist_mbid

    koito_mbid_cache: dict[int, str | None] = {}
    musicbrainz_mbid_cache: dict[str, str | None] = {}
    counter: Counter[str] = Counter()
    names_by_mbid: dict[str, str] = {}

    eligible_artists = [
        item
        for item in name_counter.most_common()
        if item[1] >= min_listen
    ]

    for normalized_name, listen_count in eligible_artists:
        artist_name = names_display[normalized_name]
        artist_mbid = inline_mbids.get(normalized_name)

        if not artist_mbid:
            koito_artist_id = koito_ids.get(normalized_name)

            if koito_artist_id is not None:
                artist_mbid = get_koito_artist_mbid(
                    session=session,
                    koito_url=koito_url,
                    koito_token=koito_token,
                    koito_artist_id=koito_artist_id,
                    cache=koito_mbid_cache,
                )

        if not artist_mbid:
            artist_mbid = lookup_artist_mbid(
                session=session,
                artist_name=artist_name,
                cache=musicbrainz_mbid_cache,
            )

        if not artist_mbid:
            continue

        counter[artist_mbid] = listen_count
        names_by_mbid[artist_mbid] = artist_name

    return [
        {
            "artist_mbid": artist_mbid,
            "artist_name": names_by_mbid[artist_mbid],
            "listen_count": listen_count,
        }
        for artist_mbid, listen_count in counter.most_common(count)
    ]


def get_top_artists_from_listenbrainz(
    session: requests.Session,
    username: str,
    time_range: str,
    count: int,
    min_listen: int,
    token: str | None = None,
) -> list[dict[str, Any]]:
    if time_range not in ALLOWED_RANGES:
        raise ValueError(
            f"Invalid TIME_RANGE: {time_range}. "
            f"Allowed: {sorted(ALLOWED_RANGES)}"
        )

    response = session.get(
        f"https://api.listenbrainz.org/1/stats/user/{username}/artists",
        params={
            "range": time_range,
            "count": min(count, 100),
        },
        headers=token_headers(token) if token else {},
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    artists = response.json()["payload"]["artists"]
    seen_mbids: set[str] = set()
    result: list[dict[str, Any]] = []

    for artist in artists:
        mbid = artist.get("artist_mbid")
        listen_count = artist.get("listen_count", 0)

        if listen_count < min_listen or not mbid or mbid in seen_mbids:
            continue

        seen_mbids.add(mbid)
        result.append(
            {
                "artist_mbid": mbid,
                "artist_name": artist.get(
                    "artist_name",
                    "Unknown Artist",
                ),
                "listen_count": listen_count,
            }
        )

    return result


def get_source_artists(
    session: requests.Session,
    source: str,
    time_range: str,
    count: int,
    min_listen: int,
) -> list[dict[str, Any]]:
    if source == "listenbrainz":
        return get_top_artists_from_listenbrainz(
            session=session,
            username=require_env("LB_USERNAME"),
            time_range=time_range,
            count=count,
            min_listen=min_listen,
            token=os.getenv("LB_TOKEN"),
        )

    if source == "koito":
        return get_top_artists_from_koito(
            session=session,
            koito_url=require_env("KOITO_URL"),
            koito_token=require_env("KOITO_TOKEN"),
            username=require_env("KOITO_USERNAME"),
            time_range=time_range,
            count=count,
            min_listen=min_listen,
        )

    raise RuntimeError(f"Unsupported SOURCE: {source}")


def add_artist_to_lidarr(
    session: requests.Session,
    lidarr_url: str,
    api_key: str,
    mbid: str,
    artist_name: str,
    root_folder: str,
    excluded_artists: set[str],
    existing_artists: set[str],
    quality_profile_id: int,
    metadata_profile_id: int,
    search_for_missing_albums: bool,
) -> bool:
    if mbid in excluded_artists or mbid in existing_artists:
        return False

    response = session.post(
        f"{lidarr_url.rstrip('/')}/api/v1/artist",
        headers=lidarr_headers(api_key),
        json={
            "foreignArtistId": mbid,
            "artistName": artist_name,
            "rootFolderPath": root_folder,
            "monitored": True,
            "qualityProfileId": quality_profile_id,
            "metadataProfileId": metadata_profile_id,
            "addOptions": {
                "searchForMissingAlbums": search_for_missing_albums,
            },
        },
        timeout=TIMEOUT,
    )

    if response.status_code in {200, 201}:
        existing_artists.add(mbid)
        return True

    if response.status_code == 400:
        logger.warning(
            "Lidarr rejected artist %s: %s",
            artist_name,
            response.text,
        )
        return False

    response.raise_for_status()
    return False


def main() -> None:
    logger.info("Starting Listenarr")

    lidarr_url = require_env("URL")
    api_key = require_env("API")
    root_folder = require_env("ROOT_FOLDER")
    source = get_source_env()
    time_range = os.getenv("TIME_RANGE", "week").strip().lower()
    count = get_int_env("COUNT", 50)
    min_listen = get_int_env("MIN_LISTEN", 5)
    add_excluded_artists = get_bool_env("ADD_EXCLUDED_ARTISTS", False)
    quality_profile_id = get_int_env("QUALITY_PROFILE_ID", 1)
    metadata_profile_id = get_int_env("METADATA_PROFILE_ID", 1)
    search_for_missing_albums = get_bool_env(
        "SEARCH_FOR_MISSING_ALBUMS",
        False,
    )

    if time_range not in ALLOWED_RANGES:
        raise RuntimeError(
            f"Invalid TIME_RANGE: {time_range}. "
            f"Allowed: {sorted(ALLOWED_RANGES)}"
        )

    session = build_session()

    excluded_artists: set[str] = set()
    if not add_excluded_artists:
        excluded_artists = get_excluded_artists(
            session=session,
            lidarr_url=lidarr_url,
            api_key=api_key,
        )

    existing_artists = get_existing_artists(
        session=session,
        lidarr_url=lidarr_url,
        api_key=api_key,
    )

    artists = get_source_artists(
        session=session,
        source=source,
        time_range=time_range,
        count=count,
        min_listen=min_listen,
    )

    added = 0
    skipped = 0

    logger.info(
        "Processing %d artists",
        len(artists),
    )

    for index, artist in enumerate(artists, start=1):
        added_ok = add_artist_to_lidarr(
            session=session,
            lidarr_url=lidarr_url,
            api_key=api_key,
            mbid=artist["artist_mbid"],
            artist_name=artist["artist_name"],
            root_folder=root_folder,
            excluded_artists=excluded_artists,
            existing_artists=existing_artists,
            quality_profile_id=quality_profile_id,
            metadata_profile_id=metadata_profile_id,
            search_for_missing_albums=search_for_missing_albums,
        )

        if added_ok:
            added += 1
        else:
            skipped += 1

    print(
        f"Done. Source: {source}. "
        f"Artists returned: {len(artists)}, "
        f"Added: {added}, skipped: {skipped}"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        logger.exception("Fatal error: %s", exc)
        sys.exit(1)
