"""Pós-processamento FFmpeg: loop seamless por crossfade, encode, pôster e SHA-256.

Estratégia de loop (Plano B — sem congelamento de frame)
--------------------------------------------------------
Dado um clipe bruto de N segundos, com ``seg`` = 2,0 s e ``xf`` = 0,5 s:

    A = [seg, 2*seg)   →  fonte 2,0 s .. 4,0 s
    B = [0,   seg)     →  fonte 0,0 s .. 2,0 s
    saída = xfade(A, B, duration=xf, offset=seg-xf)   →  2*seg - xf = 3,5 s

O clipe resultante **começa** no instante-fonte 2,0 s e **termina** no
instante-fonte 2,0 s (fim de B). Ao repetir, o último frame encosta
naturalmente no primeiro: loop contínuo, sem o "freeze" típico do
`loop=1` ou do reverse-boomerang.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional

from config import Settings
from utils import LOG, kb


class FFmpegError(RuntimeError):
    """Falha ao executar ffmpeg/ffprobe, com stderr anexado."""


class SizeBudgetError(RuntimeError):
    """O arquivo não coube no orçamento de bytes mesmo após escalar a qualidade."""


# --------------------------------------------------------------------------- #
# Resultado
# --------------------------------------------------------------------------- #
@dataclass
class ProcessResult:
    mp4_path: str
    jpg_path: str
    mp4_bytes: int
    jpg_bytes: int
    mp4_sha256: str
    jpg_sha256: str
    duration_seconds: float
    crf: int
    poster_q: int
    width: int
    height: int
    fps: int

    @property
    def mp4_kb(self) -> float:
        return kb(self.mp4_bytes)

    @property
    def jpg_kb(self) -> float:
        return kb(self.jpg_bytes)

    def to_dict(self) -> Dict[str, object]:
        data = asdict(self)
        data["mp4_kb"] = self.mp4_kb
        data["jpg_kb"] = self.jpg_kb
        return data


# --------------------------------------------------------------------------- #
# Infraestrutura de execução
# --------------------------------------------------------------------------- #
def ensure_ffmpeg(settings: Settings) -> None:
    """Falha cedo e com mensagem útil se ffmpeg/ffprobe não estiverem no PATH."""
    missing = [
        name
        for name, binary in (("ffmpeg", settings.ffmpeg_bin), ("ffprobe", settings.ffprobe_bin))
        if shutil.which(binary) is None
    ]
    if missing:
        raise FFmpegError(
            f"Binários ausentes no PATH: {', '.join(missing)}. "
            "Instale o FFmpeg (ex.: `apt-get install ffmpeg`, `brew install ffmpeg`) "
            "ou aponte FFMPEG_BIN/FFPROBE_BIN para os executáveis."
        )


def _run(cmd: List[str], *, what: str) -> subprocess.CompletedProcess:
    LOG.debug("$ %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise FFmpegError(f"{what} falhou (exit {proc.returncode}):\n{tail}")
    return proc


def probe(settings: Settings, path: Path) -> Dict[str, object]:
    """Retorna duração, dimensões e fps do primeiro stream de vídeo."""
    proc = _run(
        [
            settings.ffprobe_bin,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        what=f"ffprobe {path.name}",
    )
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise FFmpegError(f"Nenhum stream de vídeo encontrado em {path}")
    stream = streams[0]

    rate = str(stream.get("r_frame_rate", "0/1"))
    try:
        num, den = rate.split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0

    try:
        duration = float((data.get("format") or {}).get("duration", 0.0))
    except (TypeError, ValueError):
        duration = 0.0

    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "fps": fps,
        "duration": duration,
    }


# --------------------------------------------------------------------------- #
# Hash
# --------------------------------------------------------------------------- #
def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Construção do filtergraph
# --------------------------------------------------------------------------- #
def _square_chain(size: int, fps: int) -> str:
    """Normaliza qualquer proporção de entrada para um quadrado `size`×`size`.

    `force_original_aspect_ratio=increase` preenche o quadrado e o `crop`
    central descarta o excedente — sem barras pretas e sem distorção. É o que
    torna o pipeline imune ao fato de o Veo 3.x entregar 16:9 ou 9:16.
    """
    return (
        f"fps={fps},"
        f"scale={size}:{size}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={size}:{size},"
        f"setsar=1,format=yuv420p"
    )


def build_loop_filter(
    *, segment: float, crossfade: float, size: int, fps: int
) -> str:
    """Monta o filtergraph A=[seg,2seg) ⊕ B=[0,seg) com crossfade de `crossfade`s."""
    chain = _square_chain(size, fps)
    offset = round(segment - crossfade, 4)
    return (
        f"[0:v]trim=start={segment:.4f}:end={segment * 2:.4f},setpts=PTS-STARTPTS,{chain}[va];"
        f"[0:v]trim=start=0:end={segment:.4f},setpts=PTS-STARTPTS,{chain}[vb];"
        f"[va][vb]xfade=transition=fade:duration={crossfade:.4f}:offset={offset:.4f}[vout]"
    )


def _encode(
    settings: Settings,
    src: Path,
    dst: Path,
    *,
    segment: float,
    crf: int,
) -> None:
    filter_complex = build_loop_filter(
        segment=segment,
        crossfade=settings.crossfade_seconds,
        size=settings.out_size,
        fps=settings.out_fps,
    )
    cmd = [
        settings.ffmpeg_bin,
        "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(src),
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-an",                                   # requisito: sem áudio
        "-c:v", "libx264",
        "-profile:v", "main",
        "-level", "3.1",
        "-pix_fmt", "yuv420p",
        "-preset", settings.x264_preset,
        "-crf", str(crf),
        "-r", str(settings.out_fps),
        "-g", str(settings.out_fps * 2),
        "-movflags", "+faststart",
        str(dst),
    ]
    _run(cmd, what=f"encode {dst.name} (crf {crf})")


# --------------------------------------------------------------------------- #
# API principal
# --------------------------------------------------------------------------- #
def make_seamless_loop(
    settings: Settings,
    src: Path,
    dst: Path,
) -> Dict[str, object]:
    """Gera o MP4 final (loop seamless, quadrado, mudo) respeitando o teto de KB.

    Sobe o CRF em degraus até caber em `max_mp4_kb`. Retorna metadados do encode.
    """
    if not src.is_file():
        raise FileNotFoundError(f"Clipe bruto inexistente: {src}")

    info = probe(settings, src)
    duration = float(info["duration"])
    needed = settings.segment_seconds * 2

    # Se a fonte for mais curta que 2*seg, encolhe proporcionalmente os cortes
    # em vez de estourar — mantém a geometria do loop intacta.
    segment = settings.segment_seconds
    if duration < needed:
        segment = max(duration / 2.0, settings.crossfade_seconds + 0.1)
        LOG.warning(
            "Fonte com %.2fs (< %.2fs). Ajustando o segmento de corte para %.2fs.",
            duration, needed, segment,
        )
    if segment <= settings.crossfade_seconds:
        raise FFmpegError(
            f"Clipe bruto curto demais ({duration:.2f}s) para um crossfade de "
            f"{settings.crossfade_seconds}s."
        )

    dst.parent.mkdir(parents=True, exist_ok=True)
    budget = settings.max_mp4_kb * 1024
    crf = settings.crf_start
    last_size = -1

    while True:
        _encode(settings, src, dst, segment=segment, crf=crf)
        size = dst.stat().st_size
        LOG.debug("encode crf=%d → %.1f KB (teto %d KB)", crf, kb(size), settings.max_mp4_kb)
        if size <= budget:
            break
        if crf >= settings.crf_max:
            raise SizeBudgetError(
                f"{dst.name}: {kb(size)} KB acima do teto de {settings.max_mp4_kb} KB "
                f"mesmo com CRF {crf} (máximo permitido). Reduza OUT_FPS/OUT_SIZE "
                f"ou eleve MAX_MP4_KB."
            )
        LOG.info(
            "%s ficou em %.1f KB (> %d KB). Elevando CRF %d → %d.",
            dst.name, kb(size), settings.max_mp4_kb, crf, min(crf + settings.crf_step, settings.crf_max),
        )
        crf = min(crf + settings.crf_step, settings.crf_max)
        last_size = size

    out_info = probe(settings, dst)
    return {
        "crf": crf,
        "bytes": dst.stat().st_size,
        "duration": float(out_info["duration"]),
        "width": int(out_info["width"]),
        "height": int(out_info["height"]),
        "fps": settings.out_fps,
        "segment": segment,
        "previous_bytes": last_size,
    }


def extract_poster(settings: Settings, mp4: Path, jpg: Path) -> Dict[str, object]:
    """Extrai o primeiro frame do MP4 final como JPG dentro do teto de KB.

    O pôster sai do arquivo **final** (não do bruto), garantindo que seja
    pixel-a-pixel o primeiro frame do loop exibido.
    """
    jpg.parent.mkdir(parents=True, exist_ok=True)
    budget = settings.max_poster_kb * 1024
    quality = settings.poster_q_start

    while True:
        _run(
            [
                settings.ffmpeg_bin,
                "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-i", str(mp4),
                "-frames:v", "1",
                "-q:v", str(quality),
                "-f", "image2",
                str(jpg),
            ],
            what=f"poster {jpg.name} (q {quality})",
        )
        size = jpg.stat().st_size
        if size <= budget:
            return {"bytes": size, "q": quality}
        if quality >= settings.poster_q_max:
            raise SizeBudgetError(
                f"{jpg.name}: {kb(size)} KB acima do teto de {settings.max_poster_kb} KB "
                f"mesmo com -q:v {quality}."
            )
        LOG.info(
            "%s ficou em %.1f KB (> %d KB). Elevando -q:v %d → %d.",
            jpg.name, kb(size), settings.max_poster_kb, quality,
            min(quality + 2, settings.poster_q_max),
        )
        quality = min(quality + 2, settings.poster_q_max)


def process_clip(
    settings: Settings,
    raw_path: Path,
    mp4_path: Path,
    jpg_path: Path,
) -> ProcessResult:
    """Executa a cadeia completa: loop → encode → pôster → hashes."""
    encode_info = make_seamless_loop(settings, raw_path, mp4_path)
    poster_info = extract_poster(settings, mp4_path, jpg_path)

    return ProcessResult(
        mp4_path=str(mp4_path),
        jpg_path=str(jpg_path),
        mp4_bytes=int(encode_info["bytes"]),
        jpg_bytes=int(poster_info["bytes"]),
        mp4_sha256=sha256_file(mp4_path),
        jpg_sha256=sha256_file(jpg_path),
        duration_seconds=round(float(encode_info["duration"]), 3),
        crf=int(encode_info["crf"]),
        poster_q=int(poster_info["q"]),
        width=int(encode_info["width"]),
        height=int(encode_info["height"]),
        fps=int(encode_info["fps"]),
    )


# --------------------------------------------------------------------------- #
# Clipe sintético para --dry-run
# --------------------------------------------------------------------------- #
def synth_raw_clip(settings: Settings, dst: Path, *, seed: int = 0) -> Path:
    """Gera um clipe bruto sintético (sem chamar o Veo) para validar o pipeline.

    Útil em CI e no primeiro smoke test: exercita FFmpeg, Drive e planilha sem
    consumir cota da API.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    duration = max(settings.veo_duration_seconds, int(settings.segment_seconds * 2) + 1)
    _run(
        [
            settings.ffmpeg_bin,
            "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi",
            "-i", f"testsrc2=size=1280x720:rate=24:duration={duration}",
            "-vf", f"hue=h={(seed * 37) % 360}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "30",
            "-an",
            str(dst),
        ],
        what=f"synth {dst.name}",
    )
    return dst
