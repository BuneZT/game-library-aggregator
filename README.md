# Game Library Aggregator (Steam + Heroic)

A zero-dependency Linux / SteamOS Python tool that aggregates your owned PC games from **Steam** and **Heroic Games Launcher** (Epic Games, GOG, Amazon Games, Sideloaded), deduplicates cross-store titles, enriches them with review scores, genres, themes, and Linux compatibility ratings, and generates an **offline interactive HTML dashboard** (Steam-style cover tiles & table view) plus a **CSV export**.

## Features

- **Unified Library Discovery (Zero Manual Entry):**
  - **Steam:** Reads your entire owned library (installed + uninstalled) directly from Steam's local `packageinfo.vdf` + `appinfo.vdf` cache—**no Steam API key or public profile required** (with optional Steam Web API support).
  - **Heroic Games Launcher:** Reads Heroic's local JSON library caches (Flatpak and native installs) for **Epic**, **GOG**, **Amazon**, and **Sideloaded** games.
  - **Smart Deduplication:** Automatically merges games owned on multiple storefronts into a single entry with badges for each store.
- **Reviews, Genres & Themes:**
  - **IGDB API v4:** Critic Score, IGDB User Score (fallback for non-Steam exclusives), Genres, Themes, and cross-store Steam AppID matching.
  - **Steam Reviews (`IStoreBrowseService`):** Batched Steam User Review `%` and review counts (including for Epic/GOG titles that also exist on Steam).
- **Linux / Steam Deck Compatibility:**
  - **Steam Deck Verified** status (`Verified`, `Playable`, `Unsupported`, `Unknown`).
  - **ProtonDB Tier** (`Native`, `Platinum`, `Gold`, `Silver`, `Bronze`, `Borked`).
- **Interactive Dashboard (`output/games.html`) + CSV (`output/games.csv`):**
  - **Steam-style Tiles View** (portrait cover art with hover zoom, store badges, critic/user score overlays) and **Table View** (with cover thumbnails).
  - Separate sortable **Critic Score** and **User Score** columns—unrated (`—`) games stay pinned at the bottom instead of sorting as `0`.
  - Filter by **Storefront**, **Genre**, **Theme**, **Deck / ProtonDB tier**, or **No Critic Score** (great for finding niche/indie gems by User Score).
  - One-click **Export Filtered CSV** button.
- **Fast Local SQLite Cache (`cache.db`):** Caches API metadata for 30 days so future runs only fetch newly added games.
- **Zero External Dependencies:** Pure Python 3 standard library—no `pip install` required.

---

## Requirements

- Linux / SteamOS with **Python 3.10+**
- Local **Steam** and/or **Heroic Games Launcher** installation
- Free **Twitch / IGDB Developer** credentials (for Critic Scores, Genres, Themes & Heroic-to-Steam matching)

---

## Quick Setup

1. Clone the repo and create your `.env` file:
   ```bash
   git clone https://github.com/BuneZT/game-library-aggregator.git
   cd game-library-aggregator
   cp .env.example .env
   ```

2. Get free **IGDB / Twitch** credentials (~2 minutes):
   - Sign in at <https://dev.twitch.tv/console/apps> *(2FA required by Twitch)*.
   - Click **Register Your Application**:
     - **Name:** e.g. `GameLibraryAggregator`
     - **OAuth Redirect URLs:** `http://localhost`
     - **Category:** `Application Integration`
     - **Client Type:** `Confidential`
   - Copy **Client ID** and generate a **Client Secret**, then paste both into `.env`:
     ```env
     IGDB_CLIENT_ID=your_client_id
     IGDB_CLIENT_SECRET=your_client_secret
     ```

3. Run the aggregator:
   ```bash
   python3 aggregate_games.py
   ```

4. Open `output/games.html` in your browser:
   ```bash
   xdg-open output/games.html
   ```

---

## CLI Options

| Flag | Description |
| :--- | :--- |
| `--dry-run` | Discover and deduplicate libraries without calling external APIs |
| `--limit N` | Process only the first `N` games (handy for quick testing) |
| `--refresh` | Ignore the 30-day cache and re-fetch all metadata |
| `--steam-local` | Read only locally installed Steam games (`appmanifest_*.acf`) |
| `--skip-steam` | Skip Steam library |
| `--skip-heroic` | Skip Heroic library |
| `--no-compat` | Skip Steam Deck & ProtonDB lookups |
| `--output-dir DIR` | Custom output directory for `games.html` and `games.csv` |

---

## License

[MIT](LICENSE)
