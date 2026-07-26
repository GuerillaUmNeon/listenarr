# ListenBrainz / Koito to Lidarr Artist Sync

Syncs your top artists from either ListenBrainz or Koito to Lidarr automatically. It fetches artists from the selected source, filters by time range and minimum listens, skips excluded or existing artists in Lidarr, and adds the rest.

## Features

- ✅ Supports both `listenbrainz` and `koito` as source backends
- ✅ Fetches top artists from ListenBrainz stats or derives them from Koito listen history
- ✅ Respects Lidarr import exclusions and existing artists
- ✅ Configurable quality profile, metadata profile, and root folder
- ✅ Automatic retries with exponential backoff for flaky APIs
- ✅ Strict input validation and clear error messages
- ✅ Optional ListenBrainz token support
- ✅ Koito support via base URL, API token, and username

## Prerequisites

- Python 3.8+
- Lidarr v1+ with API access
- Either:
  - a ListenBrainz account, or
  - a Koito instance with API access

## Quick Start

1. **Copy `.env.example`**:

```bash
cp .env.example .env
```

2. **Edit `.env`** with your values.

### Example: Koito source

```env
URL=http://localhost:8686
API=your_lidarr_api_key_here
ROOT_FOLDER=/path/to/music/library

SOURCE=koito
KOITO_URL=http://192.168.1.22:4110
KOITO_TOKEN=your_koito_api_key
KOITO_USERNAME=your_koito_username

LB_USERNAME=
LB_TOKEN=

TIME_RANGE=week
COUNT=50
MIN_LISTEN=5
ADD_EXCLUDED_ARTISTS=False
QUALITY_PROFILE_ID=1
METADATA_PROFILE_ID=1
SEARCH_FOR_MISSING_ALBUMS=False
```

### Example: ListenBrainz source

```env
URL=http://localhost:8686
API=your_lidarr_api_key_here
ROOT_FOLDER=/path/to/music/library

SOURCE=listenbrainz
LB_USERNAME=your_listenbrainz_username
LB_TOKEN=your_listenbrainz_token

KOITO_URL=
KOITO_TOKEN=
KOITO_USERNAME=

TIME_RANGE=week
COUNT=50
MIN_LISTEN=5
ADD_EXCLUDED_ARTISTS=False
QUALITY_PROFILE_ID=1
METADATA_PROFILE_ID=1
SEARCH_FOR_MISSING_ALBUMS=False
```

3. **Install dependencies**:

```bash
pip install -r requirements.txt
```

4. **Run**:

```bash
python listenarr.py
```

## Configuration

| Variable | Default | Description |
| :-- | :-- | :-- |
| `URL` | required | Lidarr base URL, for example `http://localhost:8686` |
| `API` | required | Lidarr API key |
| `ROOT_FOLDER` | required | Music library path for new artists |
| `SOURCE` | required | Source backend: `koito` or `listenbrainz` |
| `KOITO_URL` | required for Koito | Koito base URL, for example `http://192.168.1.22:4110` |
| `KOITO_TOKEN` | required for Koito | Koito API key |
| `KOITO_USERNAME` | required for Koito | Koito username |
| `LB_USERNAME` | required for ListenBrainz | ListenBrainz username |
| `LB_TOKEN` | optional | ListenBrainz user token, recommended for authenticated requests |
| `TIME_RANGE` | `week` | One of `this_week`, `this_month`, `this_year`, `week`, `month`, `quarter`, `year`, `half_yearly`, `all_time` |
| `COUNT` | `50` | Max artists to fetch |
| `MIN_LISTEN` | `5` | Skip artists below this listen count |
| `ADD_EXCLUDED_ARTISTS` | `False` | Add artists even if they are in Lidarr import exclusions |
| `QUALITY_PROFILE_ID` | `1` | Lidarr quality profile ID |
| `METADATA_PROFILE_ID` | `1` | Lidarr metadata profile ID |
| `SEARCH_FOR_MISSING_ALBUMS` | `False` | Auto-search missing albums when adding artists |

## Source behavior

### ListenBrainz

When `SOURCE=listenbrainz`, the script uses:

- `LB_USERNAME`
- optionally `LB_TOKEN`

It fetches top artists directly from ListenBrainz statistics.

### Koito

When `SOURCE=koito`, the script uses:

- `KOITO_URL`
- `KOITO_TOKEN`
- `KOITO_USERNAME`

It fetches recent listens from Koito and derives top artists locally by counting listens per artist MBID.

## Usage Examples

**Weekly Koito sync, top 25 artists with 10+ listens**:

```env
SOURCE=koito
TIME_RANGE=this_week
COUNT=25
MIN_LISTEN=10
```

**This month ListenBrainz sync, top 50**:

```env
SOURCE=listenbrainz
TIME_RANGE=this_month
COUNT=50
```

**All-time top 100, add excluded artists too**:

```env
TIME_RANGE=all_time
COUNT=100
ADD_EXCLUDED_ARTISTS=True
```

**Quarterly sync with album search**:

```env
TIME_RANGE=quarter
SEARCH_FOR_MISSING_ALBUMS=True
```

## Output

```text
Skipping existing artist: Dead Kennedys (37c78aeb-d196-42b5-b991-6afb4fc9bc2e)
Added artist: War on Women (69c05e9a-883b-4570-9c3d-c4bfc896a488)
Skipping existing artist: Descendents (f035837e-4117-438d-a524-cacf43500e68)
Skipping existing artist: The Beatles (b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d)
Done. Source: koito. Added: 1, skipped: 3
```

## Cron Usage

Run weekly on Monday at 2AM:

```bash
0 2 * * 1 cd /path/to/script && /usr/bin/python3 listenarr.py >> sync.log 2>&1
```

## Troubleshooting

| Issue | Solution |
| :-- | :-- |
| `Missing required environment variable` | Check that the required vars for your selected `SOURCE` are present in `.env` |
| `Environment variable SOURCE must be one of ...` | Use exactly `koito` or `listenbrainz` |
| `Invalid TIME_RANGE` | Use one of `this_week`, `this_month`, `this_year`, `week`, `month`, `quarter`, `year`, `half_yearly`, `all_time` |
| `404` from Koito stats endpoint | Expected if trying ListenBrainz stats paths on Koito; use `SOURCE=koito` |
| `Bad request for artist` | Artist may already exist, be invalid, or Lidarr may reject the payload |
| `Connection timeout` | Check Lidarr or source URL accessibility |
| `No artists added` | Increase `COUNT`, lower `MIN_LISTEN`, or confirm artists have MBIDs |

## Notes

- Koito should be configured with its **base URL** only, not a manually appended `/apis/listenbrainz` path.
- ListenBrainz tokens are optional for some reads, but recommended.

## License

MIT. See [LICENSE](LICENSE) for details.