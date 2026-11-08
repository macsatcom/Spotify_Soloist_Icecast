"""ffmpeg command builder and Icecast helpers."""
from __future__ import annotations

import shlex
from urllib.parse import quote

from .config import EncoderSettings, Env

CODECS = {
    # codec: (ffmpeg args factory, container, content_type)
    "mp3": ("mp3", "audio/mpeg"),
    "aac": ("adts", "audio/aac"),
    "opus": ("ogg", "audio/ogg"),
    "vorbis": ("ogg", "audio/ogg"),
    "flac": ("ogg", "audio/ogg"),
}

PULSE_SINK = "soloist_out"


def capture_format(enc: EncoderSettings) -> str:
    """Raw PCM format captured from the Pulse monitor."""
    return "s24le" if enc.codec == "flac" and enc.bit_depth == 24 else "s16le"


def parec_cmd(enc: EncoderSettings) -> list[str]:
    return [
        "parec", f"--device={PULSE_SINK}.monitor", f"--format={capture_format(enc)}",
        f"--rate={enc.sample_rate}", "--channels=2", "--raw", "--latency-msec=50",
    ]


def icecast_url(env: Env) -> str:
    return (f"icecast://source:{quote(env.icecast_source, safe='')}@"
            f"{env.icecast_host}:{env.icecast_port}{env.icecast_mount}")


def ffmpeg_cmd(enc: EncoderSettings, env: Env) -> list[str]:
    fmt = capture_format(enc)
    container, ctype = CODECS[enc.codec]
    # Opus only supports 48k; resample if needed
    out_rate = 48000 if enc.codec == "opus" else enc.sample_rate
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-f", fmt, "-ar", str(enc.sample_rate), "-ac", "2", "-i", "pipe:0",
    ]
    if out_rate != enc.sample_rate:
        cmd += ["-af", f"aresample={out_rate}"]
    cmd += ["-ar", str(out_rate), "-ac", "2"]
    b = f"{enc.bitrate_kbps}k"
    if enc.codec == "mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", f"{min(enc.bitrate_kbps, 320)}k"]
    elif enc.codec == "aac":
        cmd += ["-c:a", "aac", "-b:a", b]
    elif enc.codec == "opus":
        cmd += ["-c:a", "libopus", "-b:a", f"{min(enc.bitrate_kbps, 510)}k", "-application", "audio"]
    elif enc.codec == "vorbis":
        cmd += ["-c:a", "libvorbis", "-b:a", b]
    elif enc.codec == "flac":
        cmd += ["-c:a", "flac", "-compression_level", str(enc.flac_compression)]
        if enc.bit_depth == 24:
            cmd += ["-sample_fmt", "s32", "-bits_per_raw_sample", "24"]
        else:
            cmd += ["-sample_fmt", "s16"]
    cmd += [
        "-content_type", ctype,
        "-ice_name", enc.stream_name,
        "-ice_description", enc.stream_description,
        "-ice_public", "0",
        "-f", container, icecast_url(env),
    ]
    return cmd


def pipeline_shell(enc: EncoderSettings, env: Env) -> str:
    """parec | ffmpeg as one shell pipeline (pipefail so either failing ends it)."""
    return "set -o pipefail; " + shlex.join(parec_cmd(enc)) + " | " + shlex.join(ffmpeg_cmd(enc, env))


def redact(s: str, env: Env) -> str:
    for secret in (env.icecast_source, quote(env.icecast_source, safe=""), env.icecast_admin_pass):
        if secret:
            s = s.replace(secret, "***")
    return s


def icecast_xml(env: Env, enc: EncoderSettings) -> str:
    from xml.sax.saxutils import escape as x
    return f"""<icecast>
  <location>Docker</location>
  <admin>admin@localhost</admin>
  <limits>
    <clients>100</clients><sources>4</sources>
    <queue-size>1048576</queue-size><client-timeout>30</client-timeout>
    <header-timeout>15</header-timeout><source-timeout>10</source-timeout>
    <burst-on-connect>1</burst-on-connect><burst-size>131072</burst-size>
  </limits>
  <authentication>
    <source-password>{x(env.icecast_source)}</source-password>
    <relay-password>{x(env.icecast_source)}</relay-password>
    <admin-user>{x(env.icecast_admin_user)}</admin-user>
    <admin-password>{x(env.icecast_admin_pass)}</admin-password>
  </authentication>
  <hostname>localhost</hostname>
  <listen-socket><port>{env.icecast_port}</port></listen-socket>
  <mount type="normal">
    <mount-name>{x(env.icecast_mount)}</mount-name>
    <stream-name>{x(enc.stream_name)}</stream-name>
    <public>0</public>
  </mount>
  <fileserve>1</fileserve>
  <paths>
    <basedir>/usr/share/icecast2</basedir>
    <logdir>/tmp/icecast-log</logdir>
    <webroot>/usr/share/icecast2/web</webroot>
    <adminroot>/usr/share/icecast2/admin</adminroot>
    <alias source="/" destination="/status.xsl"/>
  </paths>
  <logging>
    <accesslog>access.log</accesslog><errorlog>error.log</errorlog><loglevel>2</loglevel>
  </logging>
  <security>
    <chroot>0</chroot>
    <changeowner><user>icecast2</user><group>icecast</group></changeowner>
  </security>
</icecast>
"""


def supports_admin_metadata(codec: str) -> bool:
    """Icecast's admin/metadata endpoint only applies to MP3/AAC mounts."""
    return codec in ("mp3", "aac")
