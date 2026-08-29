"""Constants for the Trakt Scrobbler integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "trakt_scrobbler"

#: public API host, used when the user brings their own registered app
API_BASE: Final = "https://api.trakt.tv"
#: the host trakt.tv's own web app talks to
API_BASE_PRIVATE: Final = "https://apiz.trakt.tv"
#: where the web app is served from, used to discover its client id
APP_BASE: Final = "https://app.trakt.tv"
TRAKT_APP_URL: Final = "https://trakt.tv/oauth/applications"

# Registering an API application now requires a paid Trakt VIP membership, which
# would put this integration out of reach for most people. Trakt's own web app
# (app.trakt.tv) is a *public* OAuth client -- its OIDC discovery document
# advertises `token_endpoint_auth_methods_supported: [..., "none"]` and the
# device-code grant -- so its client id authorises the device flow on its own,
# with no client secret. That is what this integration uses by default.
#
# Consequences worth knowing: this key belongs to Trakt, not to us. They can
# rotate or rate-limit it whenever they like, and doing so would break every
# install at once. Anyone who wants a key they control can supply their own app
# during setup instead -- see AUTH_MODE_OWN_APP.
BUNDLED_CLIENT_ID: Final = (
    "201dc70c5ec6af530f12f079ea1922733f6e1085ad7b02f36d8e011b75bcea7d"
)

AUTH_MODE_BUNDLED: Final = "bundled"
AUTH_MODE_OWN_APP: Final = "own_app"

# Config entry data keys
CONF_AUTH_MODE: Final = "auth_mode"
CONF_API_BASE: Final = "api_base"
CONF_CLIENT_ID: Final = "client_id"
CONF_CLIENT_SECRET: Final = "client_secret"
CONF_TOKENS: Final = "tokens"
CONF_USERNAME: Final = "username"

# Option keys
CONF_PLAYERS: Final = "players"
CONF_EXCLUDED_APPS: Final = "excluded_apps"
CONF_MIN_DURATION: Final = "min_duration"
CONF_NEXT_EPISODE_FALLBACK: Final = "next_episode_fallback"
CONF_HEARTBEAT: Final = "heartbeat"
CONF_THUMBNAIL_MATCH: Final = "thumbnail_match"

# Option defaults
DEFAULT_MIN_DURATION: Final = 300  # seconds; ignore clips/trailers
DEFAULT_HEARTBEAT: Final = 300  # seconds between progress refreshes
DEFAULT_NEXT_EPISODE_FALLBACK: Final = True
DEFAULT_THUMBNAIL_MATCH: Final = True

# Apps that never carry scrobbleable movie/episode content. Matched
# case-insensitively as a substring of both app_name and app_id.
DEFAULT_EXCLUDED_APPS: Final[list[str]] = [
    # Infuse has its own built-in Trakt scrobbler, so anyone using it will
    # generally prefer that over this integration double-scrobbling. Excluded by
    # default; remove it from the list if you'd rather HA handle Infuse too.
    "infuse",
    "youtube",
    "music",
    "musica",
    "podcast",
    "spotify",
    "soundcloud",
    "fitness",
    "arcade",
    "app store",
    "settings",
    "impostazioni",
    "photos",
    "foto",
    "facetime",
    "testflight",
    "speedtest",
]

# media_content_type values that are never a movie or an episode.
IGNORED_CONTENT_TYPES: Final[frozenset[str]] = frozenset(
    {"music", "playlist", "channel", "game", "app", "url", "image"}
)

# How often the manager re-reads player state to advance progress.
TICK_INTERVAL: Final = 30  # seconds

# Scrobble actions
ACTION_START: Final = "start"
ACTION_PAUSE: Final = "pause"
ACTION_STOP: Final = "stop"

# Sensor states
STATE_IDLE: Final = "idle"
STATE_WATCHING: Final = "watching"
STATE_PAUSED: Final = "paused"
STATE_UNMATCHED: Final = "unmatched"
STATE_IGNORED: Final = "ignored"
STATE_ERROR: Final = "error"

# Services
SERVICE_PARSE_PREVIEW: Final = "parse_preview"
SERVICE_STOP_SCROBBLE: Final = "stop_scrobble"
SERVICE_REFRESH_TOKEN: Final = "refresh_token"
