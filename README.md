# ListenBrainz to Lidarr Artist Sync

Syncs your top artists from ListenBrainz to Lidarr automatically. Fetches your most-listened artists from a specified time range (week, month, year, all-time), filters by minimum listens, skips excluded/existing artists, and adds the rest to Lidarr.

## Features

- ✅ Fetches top artists from ListenBrainz with configurable time range and listen count
- ✅ Respects Lidarr import exclusions and existing artists
- ✅ Configurable quality/metadata profiles and root folder
- ✅ Automatic retries with exponential backoff for flaky APIs
- ✅ Strict input validation and clear error messages
- ✅ Dry-run simulation mode planned (future)


## Prerequisites

- Python 3.8+
- Lidarr v1+ with API access
- ListenBrainz account with listening stats

## Quick Start

1. **Copy `.env.example`**:

```bash
cp .env.example .env
```

2. **Edit `.env`** with your values:

```env
URL=http://localhost:8686
API=your_lidarr_api_key_here
ROOT_FOLDER=/path/to/music/library
USERNAME=your_listenbrainz_username

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
| `URL` | required | Lidarr base URL (e.g. `http://localhost:8686`) |
| `API` | required | Lidarr API key |
| `ROOT_FOLDER` | required | Music library path |
| `USERNAME` | required | ListenBrainz username |
| `TIME_RANGE` | `week` | `this_week`, `this_month`, `this_year`, `week`, `month`, `quarter`, `year`, `half_yearly`, `all_time` |
| `COUNT` | `50` | Max artists to fetch (capped at 100 by API) |
| `MIN_LISTEN` | `5` | Skip artists below this listen count |
| `ADD_EXCLUDED_ARTISTS` | `False` | Add even excluded artists |
| `QUALITY_PROFILE_ID` | `1` | Lidarr quality profile |
| `METADATA_PROFILE_ID` | `1` | Lidarr metadata profile |
| `SEARCH_FOR_MISSING_ALBUMS` | `False` | Auto-search missing albums on add |

## Usage Examples

**Weekly sync, top 25 artists with 10+ listens**:
```env
TIME_RANGE=this_week
COUNT=25
MIN_LISTEN=10
```

**This month sync, top 50**:
```env
TIME_RANGE=this_month
COUNT=50
```

**All-time top 100, add everything**:
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

```
Skipping existing artist: Dead Kennedys (37c78aeb-d196-42b5-b991-6afb4fc9bc2e)
Added artist: War on Women (69c05e9a-883b-4570-9c3d-c4bfc896a488)
Skipping existing artist: Descendents (f035837e-4117-438d-a524-cacf43500e68)
Skipping existing artist: The Beatles (b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d)
Done. Added: 1, skipped: 3
```


## Cron Usage

Run weekly on Monday at 2AM:

```bash
0 2 * * 1 cd /path/to/script && /usr/bin/python3 listenarr.py >> sync.log 2>&1
```


## Troubleshooting

| Issue | Solution |
| :-- | :-- |
| `Missing required environment variable` | Check `.env` has all required vars |
| `Invalid TIME_RANGE` | Use one of: `this_week`, `this_month`, `this_year`, `week`, `month`, `quarter`, `year`, `half_yearly`, `all_time` |
| `Bad request for artist` | Artist already exists or invalid MBID |
| `Connection timeout` | Check Lidarr URL accessibility |
| `No artists added` | Increase `COUNT` or lower `MIN_LISTEN` |


## License

MIT. See [LICENSE](LICENSE) for details.


