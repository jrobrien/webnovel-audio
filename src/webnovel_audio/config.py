from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass, field, fields


@dataclass
class General:
    lexicon: str = ""                       # optional shared pronunciation CSV
    lexicon_dir: str = "data/lexicons"      # <series-slug>.csv here is picked up by `sync`
    base_lexicon: str = "data/lexicons/_base.csv"  # always-on respellings; per-series entries win
    series_config_dir: str = "data/series"  # <series-slug>.toml overlays the base config
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
class RoyalRoad:
    library_dir: str = "library"
    state_db: str = "~/.local/state/webnovel-audio/state.db"
    request_delay: float = 2.5       # seconds between royalroad.com requests


@dataclass
class Serve:
    host: str = "0.0.0.0"
    port: int = 8080
    base_url: str = ""              # override the auto-detected http://<lan-ip>:<port>
    qr: bool = True                # print a scannable QR of the URL on startup


@dataclass
class Book:
    bitrate: str = "64k"           # AAC bitrate for .m4b packaging


@dataclass
class Audio:
    opus_bitrate: str = "56k"
    loudness_i: float = -19.0
    loudness_tp: float = -3.0
    loudness_lra: float = 11.0
    dsp: bool = True                 # apply per-voice/style effect chains ([dsp.*])


# Effect chains applied after synthesis, keyed by speaker name, voice id, or style
# (narration | thought | dialogue | system | heading). More specific keys win
# per field: speaker -> voice -> style. Fields: gain_db, semitones, hp_hz, lp_hz,
# tilt_db. These two ship as defaults so Phase 2 behaviour is unchanged.
_DSP_DEFAULTS: dict[str, dict] = {
    "thought": {"gain_db": -1.5, "hp_hz": 115.0, "lp_hz": 3600.0},
    "system": {"gain_db": -1.0, "hp_hz": 250.0, "lp_hz": 3900.0, "tilt_db": 2.0},
    "chat": {"gain_db": -3.0, "hp_hz": 300.0, "lp_hz": 3400.0, "tilt_db": 1.0},
}


def _build(dc, data: dict | None):
    names = {f.name for f in fields(dc)}
    return dc(**{k: v for k, v in (data or {}).items() if k in names})


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
    royalroad: RoyalRoad = field(default_factory=RoyalRoad)
    serve: Serve = field(default_factory=Serve)
    book: Book = field(default_factory=Book)
    dsp: dict = field(default_factory=lambda: _merge_dsp(None))
    metadata: dict = field(default_factory=dict)

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
                          ("audio", c.audio), ("royalroad", c.royalroad),
                          ("serve", c.serve), ("book", c.book)):
            allowed = {f.name for f in fields(sub)}
            for k, v in (data.get(name) or {}).items():
                if k in allowed:
                    setattr(sub, k, v)
        for key, spec in (data.get("dsp") or {}).items():
            c.dsp.setdefault(key, {}).update(spec)
        c.metadata = {**c.metadata, **(data.get("metadata") or {})}
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
            royalroad=_build(RoyalRoad, data.get("royalroad")),
            serve=_build(Serve, data.get("serve")),
            book=_build(Book, data.get("book")),
            dsp=_merge_dsp(data.get("dsp")),
            metadata=data.get("metadata", {}) or {},
        )
