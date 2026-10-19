# Plan: share listening positions with AntennaPod

Status: **parked option** (2026-10-19). Nothing is built.

## Why

The web player is desktop-first: on a phone with the screen off, Chrome on Android cannot
be relied on to go from one chapter to the next (it starts the next chapter without its
lock-screen player and has played it silently). On the phone the podcast feed in AntennaPod
is the way to listen, but then the phone and the web player keep separate places: what
you heard on the phone does not move "Continue" or the furthest-chapter marker here, and
the reverse.

## Idea

AntennaPod can sync play positions with a gpodder-compatible server (its Synchronization
settings: gpodder.net, which can point at a self-hosted server; verify custom-server
support in the current version first). The server would implement the small part of the
gpodder.net API that AntennaPod uses:

- login / device registration (one device per phone);
- subscriptions (`/api/2/subscriptions/...`): the feed URLs, so AntennaPod and the server
  agree what is subscribed (can be read-only from the server's side);
- **episode actions** (`/api/2/episodes/<user>.json`, upload and since-timestamp download):
  `play` actions with `position`, `started` and `total` per episode URL.

and map episode actions onto the existing model: the episode URL names the series and
chapter (`/audio/<slug>/<file>`), the gpodder user maps to a player profile, and a `play`
action becomes a position save (current chapter and time; furthest only moves forward, as
now). Positions saved in the web player become `play` actions AntennaPod downloads.

## Open questions

- Which AntennaPod sync backend to target (gpodder.net API vs the Nextcloud gpoddersync
  variant); what auth AntennaPod sends and whether plain HTTP on the LAN/tailnet is accepted.
- Episode identity: AntennaPod keys on the enclosure URL, which differs between the LAN and
  tailnet addresses; normalise to (slug, chapter file).
- Conflict rule when both sides moved: latest timestamp wins, but never wind furthest back.

## Size

Moderate: a handful of JSON endpoints, a device/user table, and tests against recorded
AntennaPod requests. No change to the audio or the feeds.
