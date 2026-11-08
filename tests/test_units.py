from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import encoder
from app.config import EncoderSettings, Env, Store
from app.updater import BuildInfo, detect_arch, parse_version_output


def test_env_internal_forces_localhost():
    e = Env.from_environ({"ICECAST_MODE": "internal", "ICECAST_HOST": "10.0.0.1", "ICECAST_MOUNT": "x"})
    assert e.icecast_host == "127.0.0.1" and e.icecast_mount == "/x"


def test_env_external():
    e = Env.from_environ({"ICECAST_MODE": "EXTERNAL", "ICECAST_HOST": "10.0.0.1"})
    assert e.icecast_mode == "external" and e.icecast_host == "10.0.0.1"
    assert "icecast_source" not in e.public()


def test_store_roundtrip_and_secrets(tmp_path: Path):
    s = Store(tmp_path, seed_api_key="seed")
    assert s.get_secret("soloist_api_key") == "seed"
    s.update({"encoder": {"codec": "flac", "bit_depth": 24}})
    s2 = Store(tmp_path, seed_api_key="other")
    assert s2.settings.encoder.codec == "flac" and s2.settings.encoder.bit_depth == 24
    assert s2.get_secret("soloist_api_key") == "seed"  # seed does not overwrite
    assert (tmp_path / "secrets.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(Exception):
        s.update({"encoder": {"codec": "wav"}})
    with pytest.raises(KeyError):
        s.set_secret("nope", "x")


@pytest.mark.parametrize("codec,lib,fmt", [("mp3", "libmp3lame", "mp3"), ("aac", "aac", "adts"),
                                           ("opus", "libopus", "ogg"), ("vorbis", "libvorbis", "ogg"),
                                           ("flac", "flac", "ogg")])
def test_ffmpeg_cmd(codec, lib, fmt):
    env = Env.from_environ({"ICECAST_SOURCE": "p@ss/w"})
    cmd = encoder.ffmpeg_cmd(EncoderSettings(codec=codec), env)
    assert lib in cmd and cmd[cmd.index("-f", 10) + 1] == fmt
    assert cmd[-1] == "icecast://source:p%40ss%2Fw@127.0.0.1:8100/spotify"
    if codec == "opus":
        assert cmd[cmd.index("-ar", 12) + 1] == "48000"


def test_flac_24bit_capture():
    enc = EncoderSettings(codec="flac", bit_depth=24, sample_rate=48000)
    assert "--format=s24le" in encoder.parec_cmd(enc)
    cmd = encoder.ffmpeg_cmd(enc, Env())
    assert cmd[cmd.index("-f") + 1] == "s24le" and "s32" in cmd


def test_redact():
    env = Env(icecast_source="secret1")
    assert "secret1" not in encoder.redact(encoder.pipeline_shell(EncoderSettings(), env), env)


@pytest.mark.parametrize("text,expected", [
    ("soloist 1.3.10.51 build 1793988108 (20261106) (g19412df6ea) (linux/x86_64)",
     datetime.fromtimestamp(1793988108, timezone.utc).isoformat()),
    ("soloist 1.2.3\nbuild timestamp: 2026-08-14T10:22:33Z\nplatform linux x86_64", "2026-08-14T10:22:33+00:00"),
    ("Soloist version 0.9.1 (built 2026-09-01 08:00:00, abc123)", "2026-09-01T08:00:00+00:00"),
    ("soloist v1.0 build_timestamp=1786700000", datetime.fromtimestamp(1786700000, timezone.utc).isoformat()),
    ("soloist 1.0 build 20260814-102233", "2026-08-14T10:22:33+00:00"),
])
def test_parse_version(text, expected):
    info = parse_version_output(text)
    assert info["build_time"] == expected
    assert info["version"]


def test_buildinfo_expiry():
    b = BuildInfo("x", "/x", "1", "2026-01-01T00:00:00+00:00", "2026-01-05T00:00:00+00:00")
    assert b.expires_at().date().isoformat() == "2026-04-01"
    assert round(b.days_left(datetime(2026, 3, 22, tzinfo=timezone.utc))) == 10


def test_arch():
    assert detect_arch("aarch64") == "arm64" and detect_arch("x86_64") == "x86_64"
    with pytest.raises(RuntimeError):
        detect_arch("armv7l")


def test_format_title():
    from app.core import format_title
    item = {"decorations": {"identity": {"name": "Song"},
            "creators": [{"entity": {"decorations": {"identity": {"name": "A"}}}},
                         {"entity": {"decorations": {"identity": {"name": "B"}}}}]}}
    assert format_title(item) == "A, B - Song"
