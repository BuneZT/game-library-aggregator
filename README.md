# Game Library Aggregator

Aggregate your **Steam** and **Heroic Games Launcher** (Epic, GOG, Amazon, Sideloaded) libraries into a single offline HTML dashboard and CSV—sortable by **Critic Score** and **User Score**, with genres, themes, and Steam Deck / ProtonDB compatibility.

Zero external dependencies (Python 3 stdlib only).

## Highlights

- **Auto-discovers libraries:** Reads your full owned Steam backlog from local `packageinfo.vdf` / `appinfo.vdf` *(no Steam API key needed)* + Heroic's Epic, GOG, Amazon & Sideloaded caches, merging cross-store duplicates.
- **Reviews & metadata:** IGDB (Critic Score, Genres, Themes, non-Steam User Score fallback) + Steam User Reviews (`%` & review count).
- **Linux / Deck compatibility:** Steam Deck Verified status & ProtonDB tier badges.
- **Interactive dashboard:** Steam-style cover **Tiles** & **Table** views, unrated (`—`) games pinned to the bottom when sorting, Genre/Theme/Store/Compatibility/"No Critic Score" filters, and CSV export.
- **SQLite cache (`cache.db`):** Caches results for 30 days so future runs only fetch new games.

## Quick Start

```bash
git clone https://github.com/BuneZT/game-library-aggregator.git
cd game-library-aggregator
cp .env.example .env
```

Add your free Twitch/IGDB credentials to `.env` (create a Confidential app at <https://dev.twitch.tv/console/apps> with redirect URL `http://localhost`):

```env
IGDB_CLIENT_ID=your_client_id
IGDB_CLIENT_SECRET=your_client_secret
```

Run and open the dashboard:

```bash
python3 aggregate_games.py
xdg-open output/games.html
```

## CLI Flags

- `--dry-run` — List discovered libraries without calling APIs
- `--limit N` — Process only the first `N` games
- `--refresh` — Re-fetch all cached scores and compatibility ratings
- `--steam-local` — Read installed Steam games only (`appmanifest_*.acf`)
- `--skip-steam` / `--skip-heroic` / `--no-compat` — Skip specific sources

## License

[MIT](LICENSE)
