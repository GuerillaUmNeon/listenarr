import os
import sys
import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()

TIMEOUT = (5, 30)
ALLOWED_RANGES = {"all_time", "month", "week", "year"}


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
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


def get_excluded_artists(session, lidarr_url, api_key) -> set[str]:
    url = f"{lidarr_url.rstrip('/')}/api/v1/importlistexclusion"
    response = session.get(url, headers=lidarr_headers(api_key), timeout=TIMEOUT)
    response.raise_for_status()
    return {
        item.get("foreignId")
        for item in response.json()
        if item.get("foreignId")
    }


def get_existing_artists(session, lidarr_url, api_key) -> set[str]:
    url = f"{lidarr_url.rstrip('/')}/api/v1/artist"
    response = session.get(url, headers=lidarr_headers(api_key), timeout=TIMEOUT)
    response.raise_for_status()
    return {
        item.get("foreignArtistId")
        for item in response.json()
        if item.get("foreignArtistId")
    }


def add_artist_to_lidarr(
    session,
    lidarr_url,
    api_key,
    mbid,
    artist_name,
    root_folder,
    excluded_artists,
    existing_artists,
    quality_profile_id=1,
    metadata_profile_id=1,
    search_for_missing_albums=False,
):
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


def get_top_artists(session, username, time_range, count, min_listen):
    if time_range not in ALLOWED_RANGES:
        raise ValueError(f"Invalid time_range: {time_range}. Allowed: {sorted(ALLOWED_RANGES)}")

    url = f"https://api.listenbrainz.org/1/stats/user/{username}/artists"
    params = {
        "range": time_range,
        "count": min(count, 100),
    }

    response = session.get(url, params=params, timeout=TIMEOUT)
    response.raise_for_status()

    artists = response.json()["payload"]["artists"]

    filtered = []
    seen_mbids = set()

    for artist in artists:
        mbid = artist.get("artist_mbid")
        listens = artist.get("listen_count", 0)

        if listens <= min_listen:
            continue
        if not mbid:
            continue
        if mbid in seen_mbids:
            continue

        seen_mbids.add(mbid)
        filtered.append(artist)

    return filtered


def main():
    lidarr_url = require_env("URL")
    api_key = require_env("API")
    root_folder = require_env("ROOT_FOLDER")
    username = require_env("USERNAME")

    time_range = "week"
    count = 50
    min_listen = 5
    add_excluded_artists = False

    session = build_session()

    excluded_artists = set()
    if not add_excluded_artists:
        excluded_artists = get_excluded_artists(session, lidarr_url, api_key)

    existing_artists = get_existing_artists(session, lidarr_url, api_key)
    artists = get_top_artists(session, username, time_range, count, min_listen)

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
        )
        if added_ok:
            added += 1
        else:
            skipped += 1

    print(f"Done. Added: {added}, skipped: {skipped}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)