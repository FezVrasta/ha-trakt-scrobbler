<img src="custom_components/trakt_scrobbler/brand/icon.png" alt="Trakt" width="96" align="left" />

# Trakt Scrobbler for Home Assistant

<br clear="left"/>

[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=FezVrasta&repository=ha-trakt-scrobbler&category=integration)

Scrobbles whatever your Home Assistant media players are playing to
[Trakt.tv](https://trakt.tv) — no extra app on the device, no Plex webhook, no
polling script. If Home Assistant knows what is on screen, Trakt does too.

Built and tested against a real Apple TV, but it works with anything that
exposes a `media_player` entity (Plex, Jellyfin, Kodi, Emby, Android TV…).

## What it does

- Sends `start` / `pause` / `stop` scrobbles to Trakt as playback progresses.
- Identifies the exact episode even when the player only reports a show name —
  by matching the episode thumbnail (see below).
- Tracks progress from `media_position`, or wall-clock when the player doesn't
  report one (the Apple TV often doesn't).
- Exposes a sensor per player showing what's playing, the artwork, and progress.

## Install

Click the badge above, or in HACS add
`https://github.com/FezVrasta/ha-trakt-scrobbler` as a custom **Integration**
repository, install it, and restart Home Assistant.

## Setup

**Settings → Devices & Services → Add Integration → Trakt Scrobbler.** Choose
**Sign in with Trakt** (the default), approve the code at
<https://auth.trakt.tv/activate>, and pick your media players. Done.

No Trakt API application is needed: registering one
[now requires VIP](https://trakt.tv/vip), so by default the integration signs in
as a public OAuth client using the client id Trakt's own web app ships (device
flow, no secret). The id is read from the live web app at setup, so a rotation
heals itself. If you'd rather use your own app, pick **Use my own Trakt API
application** and paste its client ID and secret.

## How the episode is identified

In order:

1. **Structured attributes** — `media_series_title` + `media_season` +
   `media_episode` (Plex, Jellyfin, Kodi).
2. **Title parsing** — the shapes players actually emit:

   | Title | Parsed as |
   | --- | --- |
   | `Spider-Noir - S1 ∙ E1 - Entra nel mio ufficio` | Spider-Noir S01E01 |
   | `Severance - S02E05 - Trojan's Horse` | Severance S02E05 |
   | `Andor 2x04 - Ever Been to Ghorman?` | Andor S02E04 |
   | `Fallout: Season 1: Episode 3: The Head` | Fallout S01E03 |
   | `Dune: Part Two (2024)` | Dune: Part Two (2024), movie |

3. **Thumbnail match** — when only a show name is available (the Apple TV app),
   the player still publishes the episode's frame as artwork, and it's the same
   frame Trakt/TMDB use. A perceptual hash compares it against the show's
   episode stills and pins the exact episode — reported as
   `match_method: thumbnail_match`. In testing the right episode scores a
   Hamming distance of ~3/256 while every other episode sits above 100, so it's
   accepted only when one is a clear winner.

> **Infuse is excluded by default** — it has its own Trakt scrobbler, so letting
> this one scrobble it too would double-count. Remove `infuse` from the ignored
> apps if you want HA to handle it.

## Options

| Option | Default | What it does |
| --- | --- | --- |
| Media players | — | Which players to watch |
| Ignored apps | Infuse, YouTube, Music, Podcasts, Spotify, Fitness… | Skip playback whose app name/ID matches |
| Minimum duration | 300 s | Anything shorter is treated as a clip and ignored |
| Progress refresh interval | 300 s | How often an ongoing scrobble is re-sent so Trakt follows seeks |
| Match episodes by thumbnail | on | Identify the episode from the player's artwork |

## Sensors

One sensor per player. Its state is `idle` / `watching` / `paused` /
`unmatched` / `ignored` / `error`, and its picture is the episode still — so a
media/glance card renders the artwork directly. Attributes include:

- **Playing** — `title`, `show_title`, `episode_title`, `episode_code`
  (`S03E09`), `season`, `episode`, `media_type`, `trakt_title`, `trakt_url`,
  `trakt_id`, `artwork`.
- **Progress** — `player_state`, `progress` (%), `position` / `remaining` /
  `duration` as `M:SS` and as `*_seconds`.
- **Match** — `match_method`, `detected`, `raw_title`, `last_action`.

```json
{
  "player_state": "playing",
  "title": "Farewell",
  "show_title": "Silo",
  "episode_code": "S03E09",
  "season": 3, "episode": 9,
  "trakt_url": "https://trakt.tv/shows/silo/seasons/3/episodes/9",
  "position": "44:32", "duration": "1:07:55", "remaining": "23:23",
  "progress": 65.6,
  "match_method": "thumbnail_match"
}
```

## Services

- **`trakt_scrobbler.parse_preview`** — show how a title/player would be matched,
  without scrobbling.
- **`trakt_scrobbler.stop_scrobble`** — end the current scrobble for a player.
- **`trakt_scrobbler.refresh_token`** — force an OAuth token refresh.

## Notes

- Trakt marks something watched when `stop` arrives at **80 %+** (Trakt's rule).
- Tokens refresh automatically; if it fails, HA raises a reauth prompt.
- Search and episode lookup use public endpoints, so `parse_preview` works even
  before you've authorised.

## Development

```bash
python3 tests/test_parser.py        # parser, dependency-free
python3 tests/test_image_match.py   # thumbnail hashing (needs Pillow)
```

The state-machine tests need the `homeassistant` package — run them inside a
container: copy `tests/test_scrobble_flow.py` into `/config` and
`python /config/test_scrobble_flow.py`.

## License

MIT
