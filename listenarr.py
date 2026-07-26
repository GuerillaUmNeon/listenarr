import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

TIMEOUT = (5, 30)
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
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_int_env(name: str, default: int) -> int:
    value = os.getenv(name, str(default))
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(
            f"Environment variable {name} must be an integer, got: {value}"
        )


def get_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise RuntimeError(f"Environment variable {name} must be a boolean, got: {value}")


def get_source_env() -> str:
    value = os.getenv("SOURCE", "").strip().lower()
    if value not in ALLOWED_SOURCES:
        raise RuntimeError(
            f"Environment variable SOURCE must be one of {sorted(ALLOWED_SOURCES)}, got: {value}"
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
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def lidarr_headers(api_key: str) -> dict:
    return {
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
    }


def token_headers(token: str) -> dict:
    return {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
    }


def get_excluded_artists(
    session: requests.Session, lidarr_url: str, api_key: str
) -> set[str]:
    url = f"{lidarr_url.rstrip('/')}/api/v1/importlistexclusion"
    response = session.get(url, headers=lidarr_headers(api_key), timeout=TIMEOUT)
    response.raise_for_status()
    return {
        item.get("foreignId")
        for item in response.json()
        if item.get("foreignId")
    }


def get_existing_artists(
    session: requests.Session, lidarr_url: str, api_key: str
) -> set[str]:
    url = f"{lidarr_url.rstrip('/')}/api/v1/artist"
    response = session.get(url, headers=lidarr_headers(api_key), timeout=TIMEOUT)
    response.raise_for_status()
    return {
        item.get("foreignArtistId")
        for item in response.json()
        if item.get("foreignArtistId")
    }


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
            "searchForMissingAlbums": search_for_missing_albums
        },
    }

    url = f"{lidarr_url.rstrip('/')}/api/v1/artist"
    response = session.post(
        url,
        headers=lidarr_headers(api_key),
        json=payload,
        timeout=TIMEOUT,
    )

    if response.status_code == 201:
        print(f"Added artist: {artist_name} ({mbid})")
        existing_artists.add(mbid)
        return True

    if response.status_code == 400:
        print(f"Bad request for {artist_name} ({mbid}): {response.text}")
        return False

    response.raise_for_status()
    return False


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
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

    raise ValueError(f"Invalid TIME_RANGE: {time_range}. Allowed: {sorted(ALLOWED_RANGES)}")


def get_top_artists_from_listenbrainz(
    session: requests.Session,
    username: str,
    time_range: str,
    count: int,
    min_listen: int,
    token: str | None = None,
) -> list[dict]:
    if time_range not in ALLOWED_RANGES:
        raise ValueError(
            f"Invalid TIME_RANGE: {time_range}. Allowed: {sorted(ALLOWED_RANGES)}"
        )

    url = f"https://api.listenbrainz.org/1/stats/user/{username}/artists"
    params = {
        "range": time_range,
        "count": min(count, 100),
    }

    headers = token_headers(token) if token else {}
    response = session.get(url, params=params, headers=headers, timeout=TIMEOUT)
    response.raise_for_status()

    data = response.json()
    artists = data["payload"]["artists"]

    filtered = []
    seen_mbids = set()

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
                "artist_name": artist.get("artist_name", "Unknown Artist"),
                "listen_count": listens,
            }
        )

    return filtered


def extract_listens(data) -> list[dict]:
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ("listens", "items", "data", "payload", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                for nested_key in ("listens", "items", "data", "results"):
                    nested_value = value.get(nested_key)
                    if isinstance(nested_value, list):
                        return nested_value

    raise RuntimeError(f"Could not find listens list in Koito response: {data}")


def extract_artist_info(listen: dict) -> tuple[str | None, str | None, datetime | None]:
    candidates = [listen]

    for key in ("track_metadata", "trackMetadata", "recording", "track", "listen"):
        value = listen.get(key)
        if isinstance(value, dict):
            candidates.append(value)

    artist_name = None
    artist_mbid = None
    listened_at = None

    timestamp_fields = [
        listen.get("listened_at"),
        listen.get("listenedAt"),
        listen.get("created_at"),
        listen.get("played_at"),
        listen.get("inserted_at"),
    ]
    for ts in timestamp_fields:
        if isinstance(ts, str):
            listened_at = parse_iso_datetime(ts)
            if listened_at:
                break
        elif isinstance(ts, (int, float)):
            listened_at = datetime.fromtimestamp(ts, tz=timezone.utc)
            break

    for obj in candidates:
        if not artist_name:
            artist_name = (
                obj.get("artist_name")
                or obj.get("artistName")
                or obj.get("name")
            )

        if not artist_mbid:
            artist_mbid = (
                obj.get("artist_mbid")
                or obj.get("artistMbid")
                or obj.get("foreignArtistId")
            )

        artist_credit = obj.get("artist_credit") or obj.get("artistCredit")
        if not artist_name and isinstance(artist_credit, list) and artist_credit:
            first = artist_credit[0]
            if isinstance(first, dict):
                artist_name = first.get("artist_name") or first.get("name")
                artist_mbid = artist_mbid or first.get("artist_mbid") or first.get("mbid")

        artists = obj.get("artists")
        if isinstance(artists, list) and artists:
            first = artists[0]
            if isinstance(first, dict):
                artist_name = artist_name or first.get("name") or first.get("artist_name")
                artist_mbid = artist_mbid or first.get("mbid") or first.get("artist_mbid")

        if artist_name and artist_mbid:
            break

    return artist_name, artist_mbid, listened_at


def fetch_recent_koito_listens(
    session: requests.Session,
    koito_url: str,
    koito_token: str,
    username: str,
    count: int,
    time_range: str,
) -> list[dict]:
    url = f"{koito_url.rstrip('/')}/apis/web/v1/listens"
    params = {
        "username": username,
        "count": max(count * 10, 200),
    }

    response = session.get(
        url,
        headers=token_headers(koito_token),
        params=params,
        timeout=TIMEOUT,
    )
    response.raise_for_status()

    listens = extract_listens(response.json())
    cutoff = get_cutoff(time_range)

    if cutoff is None:
        return listens

    filtered = []
    for listen in listens:
        _, _, listened_at = extract_artist_info(listen)
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
) -> list[dict]:
    if time_range not in ALLOWED_RANGES:
        raise ValueError(
            f"Invalid TIME_RANGE: {time_range}. Allowed: {sorted(ALLOWED_RANGES)}"
        )

    listens = fetch_recent_koito_listens(
        session=session,
        koito_url=koito_url,
        koito_token=koito_token,
        username=username,
        count=count,
        time_range=time_range,
    )

    counter = Counter()
    names_by_mbid = {}

    for listen in listens:
        artist_name, artist_mbid, _ = extract_artist_info(listen)
        if not artist_mbid:
            continue

        counter[artist_mbid] += 1
        if artist_name and artist_mbid not in names_by_mbid:
            names_by_mbid[artist_mbid] = artist_name

    top = []
    for artist_mbid, listen_count in counter.most_common(count):
        if listen_count < min_listen:
            continue
        top.append(
            {
                "artist_mbid": artist_mbid,
                "artist_name": names_by_mbid.get(artist_mbid, "Unknown Artist"),
                "listen_count": listen_count,
            }
        )

    return top


def get_source_artists(
    session: requests.Session,
    source: str,
    time_range: str,
    count: int,
    min_listen: int,
) -> list[dict]:
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


def main() -> None:
    lidarr_url = require_env("URL")
    api_key = require_env("API")
    root_folder = require_env("ROOT_FOLDER")

    source = get_source_env()
    time_range = os.getenv("TIME_RANGE", "week")
    count = get_int_env("COUNT", 50)
    min_listen = get_int_env("MIN_LISTEN", 5)
    add_excluded_artists = get_bool_env("ADD_EXCLUDED_ARTISTS", False)

    quality_profile_id = get_int_env("QUALITY_PROFILE_ID", 1)
    metadata_profile_id = get_int_env("METADATA_PROFILE_ID", 1)
    search_for_missing_albums = get_bool_env("SEARCH_FOR_MISSING_ALBUMS", False)

    session = build_session()

    excluded_artists = set()
    if not add_excluded_artists:
        excluded_artists = get_excluded_artists(session, lidarr_url, api_key)

    existing_artists = get_existing_artists(session, lidarr_url, api_key)
    artists = get_source_artists(
        session=session,
        source=source,
        time_range=time_range,
        count=count,
        min_listen=min_listen,
    )

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

    print(f"Done. Source: {source}. Added: {added}, skipped: {skipped}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)