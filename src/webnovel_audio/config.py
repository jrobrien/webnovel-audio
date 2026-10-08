from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field, fields

#: The repo checkout this package was installed from, used to anchor the
#: relative paths in `General` below. `data/` is not packaged into the wheel
#: (pyproject ships only `src/webnovel_audio`), so those paths name files in
#: the checkout, and resolving them against the *working directory* means the
#: lexicon silently disappears whenever a command runs from somewhere else.
#:
#: That is not hypothetical: a sync run from another directory rendered 23
#: chapters of one series with no base lexicon at all, saying "ky" for every
#: `qi`, and the only trace was an empty `base_lexicon_sha256` in the
#: manifest. See `resolve_data_path`.
_CHECKOUT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def resolve_data_path(path: str) -> str:
    """Absolute location of one of the repo-relative paths in `General`.

    Tries the working directory first, so an explicit relative path still
    means what the caller typed, then falls back to the checkout. Returns ""
    for an empty setting -- that means "disabled", which is different from
    "configured but missing" and must not be conflated with it.

    The path is returned whether or not it exists: existence is the caller's
    business, and a caller that treats "missing" as "disabled" is exactly the
    bug this function was written for.
    """
    path = os.path.expanduser(path or "")
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    if os.path.exists(path):
        return os.path.abspath(path)
    return os.path.join(_CHECKOUT_ROOT, path)


@dataclass
class General:
    lexicon: str = ""                       # optional shared pronunciation CSV
    lexicon_dir: str = "data/lexicons"      # <series-slug>.csv here is picked up by `sync`
    base_lexicon: str = "data/lexicons/_base.csv"  # always-on respellings; per-series entries win
    series_config_dir: str = "data/series"  # <series-slug>.toml overlays the base config
    tagger: str = ""                        # spaCy model for POS rules; "" = the default model
    cache_dir: str = ".cache/segments"
    models_dir: str = os.path.expanduser("~/.cache/webnovel-audio")
    speak_title: bool = True
    speak_series: bool = True    # prefix the chapter announcement with the series title


@dataclass
class Voices:
    # Fallback voices used when [cast] does not name a speaker.
    narrator: str = "am_michael"
    thought: str = "bm_george"
    dialogue_default: str = "am_michael"
    system_ui: str = "af_sky"
    machine: str = ""               # `machine` style (see Synth.machine_marker); "" = system_ui


@dataclass
class Cast:
    protagonist: str = ""            # POV character; catches untagged first-person lines
    narrator: str = ""              # overrides voices.narrator when set
    default: str = ""               # voice for dialogue whose speaker isn't mapped
    voices: dict = field(default_factory=dict)   # character name -> Kokoro voice id
    seed_chapters: int = 5           # `series add` samples this many chapters to seed
                                      # a per-series [cast.voices] starter; 0 disables it


@dataclass
class Chat:
    """Livestream 'chat' overlay: [Handle (Location): message] lines."""
    speak_username: str = "first"    # always | first | never (first time per chapter)
    speak_location: bool = False
    dampen_caps: bool = True         # "SO SCIFI" -> "So Scifi"
    earcon: bool = True              # short blip before each run of chat lines
    rate: float = 1.10               # chat reads a little quicker than prose
    voices: list = field(default_factory=lambda: [
        "af_nicole", "am_eric", "bf_lily", "am_liam", "af_river", "bm_daniel",
        "af_alloy", "am_fenrir", "bf_isabella", "am_onyx", "af_aoede", "bm_fable",
    ])
    voices_by_user: dict = field(default_factory=dict)   # handle -> pinned voice id


@dataclass
class Synth:
    backend: str = "kokoro"
    speed: float = 1.0
    sample_rate: int = 24000
    system_rate: float = 1.06        # LitRPG status boxes read a touch faster
    thought_threshold: float = 0.6   # italic char coverage to call a sentence "thought"
    # Delimiter an author wraps a machine voice in (an AI, a system talking in
    # the MC's head): `//Awaiting input…//`. Text between a pair -- inline, or
    # a run of paragraphs -- is read as style `machine`. "" = off; set it per
    # series, since the same characters mean nothing special elsewhere.
    machine_marker: str = ""
    machine_rate: float = 1.0


@dataclass
class Pauses:
    sentence_ms: int = 260
    paragraph_ms: int = 380
    scene_break_ms: int = 1100
    dialogue_ms: int = 240
    chat_ms: int = 200               # between consecutive chat messages
    ellipsis_extra_ms: int = 220
    lead_ms: int = 1000        # settling silence before the first word
    tail_ms: int = 900


@dataclass
class Library:
    library_dir: str = "library"
    state_db: str = "~/.local/state/webnovel-audio/state.db"


@dataclass
class Serve:
    host: str = "0.0.0.0"
    port: int = 8080
    base_url: str = ""              # override the auto-detected http://<lan-ip>:<port>
    # This machine's Tailscale address, e.g. "http://host.tailnet.ts.net:8080".
    # `serve` prints a second QR code for it at startup.
    tailnet_url: str = ""
    # Temporary: 301 every feed requested on any other host to `tailnet_url`.
    # Podcast apps (AntennaPod) rewrite a subscription's stored URL on a
    # permanent redirect, which is how an existing one is moved. Turn it off
    # again once the phone has refreshed.
    redirect_to_tailnet: bool = False
    # One line per request (feeds, the index, redirects; audio and cover fetches
    # once per client per 10 minutes), in a rotating file capped near 4 MB.
    # "" turns it off.
    access_log: str = "~/.local/state/webnovel-audio/access.log"
    qr: bool = True                # print a scannable QR of the URL on startup


@dataclass
class Book:
    bitrate: str = "64k"           # AAC bitrate for .m4b packaging


@dataclass
class Audio:
    opus_bitrate: str = "32k"
    loudness_i: float = -16.0
    loudness_tp: float = -1.0
    loudness_lra: float = 11.0
    dsp: bool = True                 # apply per-voice/style effect chains ([dsp.*])
    # "fast": one gain + peak limiter to the target loudness (~5x quicker, within
    # ~0.4 LU of loudnorm). "loudnorm": ffmpeg's two-pass loudnorm filter.
    mastering: str = "fast"
    # chapters mastered at once while the next one synthesizes (each ffmpeg is
    # single-threaded; the synth uses the rest of the cores). 1 = no overlap.
    master_jobs: int = 3


# Effect chains applied after synthesis, keyed by speaker name, voice id, or style
# (narration | thought | dialogue | system | machine | heading). More specific keys
# win per field: speaker -> voice -> style. Fields: gain_db, semitones, hp_hz, lp_hz,
# tilt_db, robot_hz/robot_mix, ring_hz/ring_mix. The defaults below ship built in.
_DSP_DEFAULTS: dict[str, dict] = {
    "thought": {"gain_db": -1.5, "hp_hz": 115.0, "lp_hz": 3600.0},
    "system": {"gain_db": -1.0, "hp_hz": 250.0, "lp_hz": 3900.0, "tilt_db": 2.0},
    "chat": {"gain_db": -3.0, "hp_hz": 300.0, "lp_hz": 3400.0, "tilt_db": 1.0},
    # monotone vocoder robot, a little ring-mod grit, band-limited like a speaker
    "machine": {"robot_hz": 110.0, "robot_mix": 0.85, "ring_hz": 55.0, "ring_mix": 0.25,
                "hp_hz": 180.0, "lp_hz": 5000.0, "tilt_db": 1.0},
}


def _build(dc, data: dict | None):
    names = {f.name for f in fields(dc)}
    return dc(**{k: v for k, v in (data or {}).items() if k in names})


_CORE_SECTIONS = {"general", "voices", "cast", "chat", "synth", "pauses", "audio",
                  "library", "serve", "book", "dsp", "metadata"}


def _provider_sections(data: dict) -> dict:
    """Every other top-level table belongs to a content provider, keyed by
    the provider's name. Core never reads inside them."""
    out = {}
    for name, table in data.items():
        if name in _CORE_SECTIONS or not isinstance(table, dict):
            continue
        stale = sorted({"library_dir", "state_db"} & set(table))
        if stale:
            raise SystemExit(f"config: {', '.join(stale)} now belong under [library], "
                             f"not [{name}]")
        out[name] = dict(table)
    return out


def _merge_dsp(user: dict | None) -> dict:
    out = {k: dict(v) for k, v in _DSP_DEFAULTS.items()}
    for key, spec in (user or {}).items():
        out.setdefault(key, {}).update(spec)
    return out


@dataclass
class Config:
    general: General = field(default_factory=General)
    voices: Voices = field(default_factory=Voices)
    cast: Cast = field(default_factory=Cast)
    chat: Chat = field(default_factory=Chat)
    synth: Synth = field(default_factory=Synth)
    pauses: Pauses = field(default_factory=Pauses)
    audio: Audio = field(default_factory=Audio)
    library: Library = field(default_factory=Library)
    serve: Serve = field(default_factory=Serve)
    book: Book = field(default_factory=Book)
    dsp: dict = field(default_factory=lambda: _merge_dsp(None))
    metadata: dict = field(default_factory=dict)
    providers: dict = field(default_factory=dict)   # [<provider name>] tables

    def overlay(self, path: str | None) -> "Config":
        """Return a copy of this config with the TOML at `path` merged over it.

        Per-series `data/series/<slug>.toml` uses this: only the keys present in
        the overlay change; a `[cast.voices]` / `[chat.voices]` in the overlay
        replaces that table wholesale, `[dsp.*]` merges per key.
        """
        if not path or not os.path.exists(path):
            return self
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        c = copy.deepcopy(self)
        for name, sub in (("general", c.general), ("voices", c.voices), ("cast", c.cast),
                          ("chat", c.chat), ("synth", c.synth), ("pauses", c.pauses),
                          ("audio", c.audio), ("library", c.library),
                          ("serve", c.serve), ("book", c.book)):
            allowed = {f.name for f in fields(sub)}
            for k, v in (data.get(name) or {}).items():
                if k in allowed:
                    setattr(sub, k, v)
        for key, spec in (data.get("dsp") or {}).items():
            c.dsp.setdefault(key, {}).update(spec)
        c.metadata = {**c.metadata, **(data.get("metadata") or {})}
        for name, table in _provider_sections(data).items():
            c.providers.setdefault(name, {}).update(table)
        return c

    @classmethod
    def load(cls, path: str | None) -> "Config":
        if not path or not os.path.exists(path):
            return cls()
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        audio = _build(Audio, data.get("audio"))
        if data.get("audio", {}).get("thought_dsp") is not None:  # back-compat alias
            audio.dsp = bool(data["audio"]["thought_dsp"])
        return cls(
            general=_build(General, data.get("general")),
            voices=_build(Voices, data.get("voices")),
            cast=_build(Cast, data.get("cast")),
            chat=_build(Chat, data.get("chat")),
            synth=_build(Synth, data.get("synth")),
            pauses=_build(Pauses, data.get("pauses")),
            audio=audio,
            library=_build(Library, data.get("library")),
            serve=_build(Serve, data.get("serve")),
            book=_build(Book, data.get("book")),
            dsp=_merge_dsp(data.get("dsp")),
            metadata=data.get("metadata", {}) or {},
            providers=_provider_sections(data),
        )
