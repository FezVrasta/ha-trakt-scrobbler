<img src="brands/icon.png" alt="Trakt" width="96" align="left" />

# Trakt Scrobbler for Home Assistant

<br clear="left"/>

Scrobbles whatever your Home Assistant media players are playing to
[Trakt.tv](https://trakt.tv) — no extra app on the device, no Plex webhook, no
polling script. If Home Assistant knows what is on screen, Trakt does too.

Built against a real Apple TV, so it copes with the messy way tvOS reports
playback, but it works with anything that exposes a `media_player` entity
(Plex, Jellyfin, Kodi, Emby, Android TV…).

## What it does

- Watches the media players you choose and sends `start` / `pause` / `stop`
  scrobbles to Trakt as playback progresses.
- Figures out *what* is playing from whatever the player gives it — structured
  attributes when available, otherwise by parsing the title string.
- Tracks progress from `media_position` when the player reports one, and falls
  back to wall-clock accounting when it doesn't (the Apple TV often doesn't).
- Exposes a sensor per player so you can see exactly what was matched, how, and
  how far along it is.

## Installation

### HACS

1. HACS → ⋮ → **Custom repositories**
2. Add this repository with category **Integration**
3. Install **Trakt Scrobbler**, then restart Home Assistant

### Manual

Copy `custom_components/trakt_scrobbler` into your Home Assistant `config/custom_components/`
directory and restart.

## Setup

**Settings → Devices & Services → Add Integration → Trakt Scrobbler**, then pick
one of two ways to authorise:

### Sign in with Trakt (default, no API application)

Registering a Trakt API application [now requires a paid VIP
membership](https://trakt.tv/vip), which would put this integration out of reach
for most people. So by default it authorises as a **public OAuth client** using
the client id that Trakt's own web app (`app.trakt.tv`) ships.

That is a supported configuration, not a workaround of the protocol: Trakt's
OIDC discovery document advertises

```
token_endpoint_auth_methods_supported: ["client_secret_basic", "client_secret_post", "none"]
grant_types_supported:                 [..., "urn:ietf:params:oauth:grant-type:device_code"]
scopes_supported:                      [..., "offline_access"]
```

`none` means the client authenticates without a secret, which is exactly what
the device-code grant is designed for. You get a code, approve it at
<https://auth.trakt.tv/activate>, and the flow completes — nothing else to set up.

#### The id is looked up, not hardcoded

That client id belongs to Trakt, not to this integration, so they can rotate it
whenever they like. Rather than freeze a copy into this repo and break on the
day that happens, the integration reads the current one out of the web app's
published JavaScript.

It costs nothing in the normal case. On setup and on every reload the id in use
is checked with one small unauthenticated request (~0.1 s). Only when that check
*fails* does it crawl `app.trakt.tv`'s script chunks for the current value,
which takes about a second, and the result is saved to the config entry. The
value in `const.py` is a seed used when the lookup cannot be completed at all —
not the source of truth.

So a rotation heals itself on the next reload. Diagnostics report
`client_id_is_bundled_seed` so you can tell which one you are on.

**The remaining trade-off:** this is still Trakt's key, shared by every install,
so they can rate-limit or revoke it. It also rather obviously routes around the
VIP gate on app registration. If either bothers you, use the other option.

### Use my own Trakt API application

If you have VIP and an app of your own, paste its client ID and secret. The
credentials live on the config entry and the entry is keyed by Trakt username,
so several accounts can each add their own alongside one another. This is the
durable choice — it is a key you control.


## How titles get matched

Different players describe playback very differently. The integration tries, in
order:

1. **Structured attributes.** `media_series_title` + `media_season` +
   `media_episode` (Plex, Jellyfin, Kodi) are trusted above everything else.
2. **`media_content_type: movie`** — matched as a film.
3. **Title parsing.** Handles the shapes players actually emit:

   | Title | Parsed as |
   | --- | --- |
   | `Spider-Noir - S1 ∙ E1 - Entra nel mio ufficio` | Spider-Noir S01E01 |
   | `Severance - S02E05 - Trojan's Horse` | Severance S02E05 |
   | `Andor 2x04 - Ever Been to Ghorman?` | Andor S02E04 |
   | `Fallout: Season 1: Episode 3: The Head` | Fallout S01E03 |
   | `Dune: Part Two (2024)` | Dune: Part Two (2024), movie |

4. **A bare name** like `Ted Lasso` — which is what the built-in Apple TV app
   reports. Trakt is asked whether it knows a film or a show by that name, using
   the reported runtime as a tie-breaker. If it's a show, the **next unwatched
   episode** from your Trakt progress is scrobbled (see below).

> **Infuse is excluded by default.** It has its own built-in Trakt scrobbler, so
> letting this integration scrobble it too would double-count. Its title format
> is still handled (the parser row above) for anyone who removes `infuse` from
> the ignored-apps list.

### The next-episode guess

The Apple TV's own TV app reports only the series name — never a season or
episode number. Without a fallback, none of it would ever scrobble.

When **Guess the episode** is on (the default), the integration asks Trakt for
your watched progress on that show and scrobbles whatever comes next. That is
almost always right if you watch in order, and wrong if you skip around.

Every guessed match is flagged: the sensor's `episode_guessed` attribute is
`true` and `match_method` reads `next_episode_guess`. Turn the option off in the
integration's settings if you'd rather skip those instead of guessing.

## Options

| Option | Default | What it does |
| --- | --- | --- |
| Media players | — | Which players to watch |
| Ignored apps | Infuse, YouTube, Music, Podcasts, Spotify, Fitness… | Playback is skipped when the string matches the app name or ID |
| Minimum duration | 300 s | Anything shorter is treated as a clip and ignored |
| Progress refresh interval | 300 s | How often an ongoing scrobble is re-sent so Trakt follows seeks |
| Guess the episode | on | The next-episode fallback described above |

Watch the logs (`custom_components.trakt_scrobbler` at `info` level) or the
sensor attributes for a while to confirm the matches — especially the guessed
episodes — look right.

## Sensors

One sensor per configured player, with states `idle`, `watching`, `paused`,
`unmatched`, `ignored` and `error`, plus attributes:

`detected`, `raw_title`, `parse_method`, `trakt_title`, `trakt_url`, `trakt_id`,
`match_method`, `episode_guessed`, `media_type`, `season`, `episode`,
`progress`, `elapsed`, `duration`, `last_action`, `reason`, `error`.

## Services

### `trakt_scrobbler.parse_preview`

Answers "what would you do with this?" without touching your history. Takes a
player, a raw title, or both, and returns the parse plus the Trakt match.

```yaml
action: trakt_scrobbler.parse_preview
data:
  title: "Spider-Noir - S1 ∙ E1 - Entra nel mio ufficio"
  media_duration: 2815
```

```yaml
action: trakt_scrobbler.parse_preview
data:
  player: media_player.tv
```

### `trakt_scrobbler.stop_scrobble`

Ends the scrobble in progress for a player, submitting its current progress.

### `trakt_scrobbler.refresh_token`

Forces an OAuth token refresh. Refreshes happen automatically; this is for
troubleshooting.

## Notes

- Trakt marks something watched when a `stop` arrives at **80 % or more**. That
  threshold is Trakt's, not this integration's.
- Tokens are refreshed automatically (`offline_access`). If a refresh ever
  fails, Home Assistant raises a reauth prompt.
- Matching (search and episode lookup) uses public endpoints, so titles resolve
  even before you have authorised — which is what makes `parse_preview` useful
  as a first check.
- Scrobbles that hit a Trakt rate limit are retried on the next tick rather than
  dropped.

## Development

The parser tests are dependency-free and include the exact attribute payloads a
real Apple TV emits for the built-in TV app and for Infuse:

```bash
python3 tests/test_parser.py
```

The scrobble state machine is tested against a stubbed Trakt, so it needs the
`homeassistant` package. Easiest inside a running container:

```bash
docker cp tests/test_scrobble_flow.py homeassistant:/config/
docker exec homeassistant python /config/test_scrobble_flow.py
```

It covers the whole play → pause → resume → stop cycle, progress accounting with
and without `media_position`, item switching, app/duration filtering, and
both branches of the next-episode guess.

## License

MIT
