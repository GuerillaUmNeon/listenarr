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
        status_forcelist=[429, 500, 502, 503, 504],
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
    url = f"{lidarr_url.rstrip('/')}/api/v1/importlistexclusion"

    response = session.get(
        url,
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
    url = f"{lidarr_url.rstrip('/')}/api/v1/artist"

    response = session.get(
        url,
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

    url = "https://musicbrainz.org/ws/2/artist"
    params = {
        "query": f'artist:"{artist_name}"',
        "fmt": "json",
        "limit": 10,
    }
    headers = {
        "User-Agent": (
            "listenarr/1.0 "
            "(personal music automation)"
        ),
        "Accept": "application/json",
    }

    try:
        response = session.get(
            url,
            params=params,
            headers=headers,
            timeout=TIMEOUT,
        )

        if response.status_code == 503:
            print(f"MusicBrainz rate limit reached for {artist_name}")
            time.sleep(2)
            cache[normalized_name] = None
            return None

        response.raise_for_status()
        data = response.json()

    except requests.RequestException as exc:
        print(f"MusicBrainz lookup failed for {artist_name}: {exc}")
        cache[normalized_name] = None
        return None

    results = data.get("artists", [])

    if not results:
        print(f"No MusicBrainz result for {artist_name}")
        cache[normalized_name] = None
        return None

    exact_matches = [
        artist
        for artist in results
        if artist.get("name", "").casefold() == normalized_name
    ]

    candidates = exact_matches or results
    mbid = candidates[0].get("id")

    if not is_valid_mbid(mbid):
        print(f"Invalid MusicBrainz MBID for {artist_name}: {mbid!r}")
        cache[normalized_name] = None
        return None

    print(f"Resolved {artist_name} -> {mbid}")
    cache[normalized_name] = mbid

    # MusicBrainz asks clients to respect its public API rate limit.
    time.sleep(1.1)

    return mbid


def extract_artist_info(
    listen: dict[str, Any],
) -> tuple[str | None, str | None, datetime | None]:
    listened_at = parse_timestamp(listen.get("time"))

    artist_name: str | None = None
    artist_mbid: str | None = None

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

                # Koito's numeric id is internal and is not an MBID.
                artist_mbid = (
                    first_artist.get("mbid")
                    or first_artist.get("artist_mbid")
                    or first_artist.get("artistMbid")
                )

        artist_name = (
            artist_name
            or track.get("artist_name")
            or track.get("artistName")
        )

        artist_mbid = (
            artist_mbid
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
        or listen.get("artist_mbid")
        or listen.get("artistMbid")
    )

    if artist_name is not None:
        artist_name = str(artist_name)

    if artist_mbid is not None:
        artist_mbid = str(artist_mbid)

    return artist_name, artist_mbid, listened_at


def extract_listens(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]

    if not isinstance(data, dict):
        raise RuntimeError(
            f"Could not find listens list in Koito response: {data}"
        )

    items = data.get("items")

    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]

    raise RuntimeError(
        f"Could not find 'items' list in Koito response: {data}"
    )


def fetch_recent_koito_listens(
    session: requests.Session,
    koito_url: str,
    koito_token: str,
    username: str,
    count: int,
    time_range: str,
) -> list[dict[str, Any]]:
    del username  # Koito identifies the account from the API token.

    url = f"{koito_url.rstrip('/')}/apis/web/v1/listens"
    cutoff = get_cutoff(time_range)
    target_count = max(count * 10, 200)
    page = 1
    listens: list[dict[str, Any]] = []

    while len(listens) < target_count:
        params = {
            "page": page,
            "period": time_range,
        }

        response = session.get(
            url,
            headers=token_headers(koito_token),
            params=params,
            timeout=TIMEOUT,
        )
        response.raise_for_status()

        body = response.json()

        print(f"Koito request URL: {response.url}")
        print(f"Koito HTTP status: {response.status_code}")

        if isinstance(body, dict):
            print(f"Koito response keys: {sorted(body.keys())}")

        page_items = extract_listens(body)

        print(
            f"Koito page {page}: {len(page_items)} items, "
            f"total={body.get('total_record_count')}, "
            f"has_next_page={body.get('has_next_page')}"
        )

        if not page_items:
            break

        listens.extend(page_items)

        if not body.get("has_next_page", False):
            break

        page += 1

    print(f"Koito total listens collected: {len(listens)}")

    if listens:
        print(f"First Koito listen: {listens[0]}")

    if cutoff is None:
        return listens

    filtered: list[dict[str, Any]] = []
    unknown_timestamp_count = 0

    for listen in listens:
        _, _, listened_at = extract_artist_info(listen)

        if listened_at is None:
            unknown_timestamp_count += 1

        if listened_at is None or listened_at >= cutoff:
            filtered.append(listen)

    print(f"Koito listens after time filter: {len(filtered)}")
    print(
        "Koito listens with unknown timestamp: "
        f"{unknown_timestamp_count}"
    )

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

    for listen in listens:
        artist_name, _, _ = extract_artist_info(listen)

        if not artist_name:
            continue

        normalized_name = artist_name.casefold()
        name_counter[normalized_name] += 1
        names_display[normalized_name] = artist_name

    print(f"Unique Koito artist names: {len(name_counter)}")

    mbid_cache: dict[str, str | None] = {}
    counter: Counter[str] = Counter()
    names_by_mbid: dict[str, str] = {}

    for normalized_name, listen_count in name_counter.most_common():
        if listen_count < min_listen:
            continue

        artist_name = names_display[normalized_name]
        artist_mbid = lookup_artist_mbid(
            session=session,
            artist_name=artist_name,
            cache=mbid_cache,
        )

        if not artist_mbid:
            print(f"Skipping unresolved artist: {artist_name}")
            continue

        counter[artist_mbid] = listen_count
        names_by_mbid[artist_mbid] = artist_name

    top: list[dict[str, Any]] = []

    for artist_mbid, listen_count in counter.most_common(count):
        top.append(
            {
                "artist_mbid": artist_mbid,
                "artist_name": names_by_mbid[artist_mbid],
                "listen_count": listen_count,
            }
        )

    print(f"Artists resolved to MusicBrainz MBIDs: {len(top)}")
    return top


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

    url = f"https://api.listenbrainz.org/1/stats/user/{username}/artists"
    params = {
        "range": time_range,
        "count": min(count, 100),
    }

    headers = token_headers(token) if token else {}
    response = session.get(
        url,
        params=params,
        headers=headers,
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    data = response.json()
    artists = data["payload"]["artists"]

    filtered: list[dict[str, Any]] = []
    seen_mbids: set[str] = set()

    for artist in artists:
        mbid = artist.get("artist_mbid")
        listens = artist.get("listen_count", 0)

        if listens < min_listen:
            continue
        if not mbid:
            continue
        if mbid in seen_mbids:
            continue

        seen_mbids.add(mbid)
        filtered.append(
            {
                "artist_mbid": mbid,
                "artist_name": artist.get(
                    "artist_name",
                    "Unknown Artist",
                ),
                "listen_count": listens,
            }
        )

    return filtered


def get_source_artists(
    session: requests.Session,
    source: str,
    time_range: str,
    count: int,
    min_listen: int,
) -> list[dict[str, Any]]:
    if source == "listenbrainz":
        username = require_env("LB_USERNAME")
        token = os.getenv("LB_TOKEN")

        return get_top_artists_from_listenbrainz(
            session=session,
            username=username,
            time_range=time_range,
            count=count,
            min_listen=min_listen,
            token=token,
        )

    if source == "koito":
        username = require_env("KOITO_USERNAME")
        koito_url = require_env("KOITO_URL")
        koito_token = require_env("KOITO_TOKEN")

        return get_top_artists_from_koito(
            session=session,
            koito_url=koito_url,
            koito_token=koito_token,
            username=username,
            time_range=time_range,
            count=count,
            min_listen=min_listen,
        )

    raise RuntimeError(f"Unsupported SOURCE: {source}")


def add_artist_to_lidarr(
    session: requests.Session,
    lidarr_url: str,
    api_key: str,
    mbid: str | None,
    artist_name: str,
    root_folder: str,
    excluded_artists: set[str],
    existing_artists: set[str],
    quality_profile_id: int,
    metadata_profile_id: int,
    search_for_missing_albums: bool,
) -> bool:
    if not mbid:
        print(f"Skipping {artist_name}: missing MBID")
        return False

    if not is_valid_mbid(mbid):
        print(f"Skipping {artist_name}: invalid MBID {mbid}")
        return False

    if mbid in excluded_artists:
        print(f"Skipping excluded artist: {artist_name} ({mbid})")
        return False

    if mbid in existing_artists:
        print(f"Skipping existing artist: {artist_name} ({mbid})")
        return False

    payload = {
        "foreignArtistId": mbid,
        "artistName": artist_name,
        "rootFolderPath": root_folder,
        "monitored": True,
        "qualityProfileId": quality_profile_id,
        "metadataProfileId": metadata_profile_id,
        "addOptions": {
            "searchForMissingAlbums": search_for_missing_albums,
        },
    }

    url = f"{lidarr_url.rstrip('/')}/api/v1/artist"
    response = session.post(
        url,
        headers=lidarr_headers(api_key),
        json=payload,
        timeout=TIMEOUT,
    )

    if response.status_code in {200, 201}:
        print(f"Added artist: {artist_name} ({mbid})")
        existing_artists.add(mbid)
        return True

    if response.status_code == 400:
        print(
            f"Bad request for {artist_name} ({mbid}): "
            f"{response.text}"
        )
        return False

    response.raise_for_status()
    return False


def main() -> None:
    lidarr_url = require_env("URL")
    api_key = require_env("API")
    root_folder = require_env("ROOT_FOLDER")

    source = get_source_env()
    time_range = os.getenv("TIME_RANGE", "week").strip().lower()
    count = get_int_env("COUNT", 50)
    min_listen = get_int_env("MIN_LISTEN", 5)
    add_excluded_artists = get_bool_env(
        "ADD_EXCLUDED_ARTISTS",
        False,
    )

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

    print(f"Artists returned from source: {len(artists)}")

    added = 0
    skipped = 0

    for artist in artists:
        added_ok = add_artist_to_lidarr(
            session=session,
            lidarr_url=lidarr_url,
            api_key=api_key,
            mbid=artist.get("artist_mbid"),
            artist_name=artist.get("artist_name", "Unknown Artist"),
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
        print(f"Fatal error: {exc}", file=sys.stderr)
        sys.exit(1)
