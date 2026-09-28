#!/usr/bin/env python3
"""Pipeline autônomo de ectoscopia clínica — Veo 3.1 → FFmpeg → Drive → Planilha.

Executa, sem intervenção manual, a matriz completa de 4 personas × 5 estados
(20 clipes):

    1. Gera o clipe no Google Veo 3.1 (`generator.py`)
    2. Costura o loop seamless, normaliza para 720×720/24fps/mudo e extrai o
       pôster JPG, calculando SHA-256 de ambos (`processor.py`)
    3. Cria `medical-assets/ectoscopia/<Ator>/` no Drive e sobe os artefatos
       (`drive_manager.py`)
    4. Atualiza a planilha de controle em tempo real (`tracker.py`)

Uso
---
    python run_pipeline.py --dry-run --skip-drive        # smoke test local
    python run_pipeline.py                               # matriz completa
    python run_pipeline.py --actors A01 A03 --states basal dispneia
    python run_pipeline.py --force --tracker sheets

A execução é **idempotente e retomável**: clipes já concluídos (registrados no
`manifest.json` do ator e presentes em disco) são pulados, a menos que
`--force` seja usado.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import prompts_data
from config import Settings, load_settings
from drive_manager import DriveError, DriveManager, NullDriveManager, UploadedFile
from generator import VeoError, VeoGenerator
from processor import (
    FFmpegError,
    ProcessResult,
    SizeBudgetError,
    ensure_ffmpeg,
    process_clip,
    synth_raw_clip,
)
from tracker import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    BaseTracker,
    RowUpdate,
    build_tracker,
)
from utils import LOG, Progress, human_size, iso_timestamp, setup_logging, timestamp

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = "1.1"


# --------------------------------------------------------------------------- #
# Resultado por clipe
# --------------------------------------------------------------------------- #
@dataclass
class ClipOutcome:
    actor_id: str
    state_id: str
    status: str
    mp4_kb: Optional[float] = None
    video_link: str = ""
    poster_link: str = ""
    sha256: str = ""
    error: str = ""


@dataclass
class RunSummary:
    outcomes: List[ClipOutcome] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status == status)

    @property
    def failures(self) -> List[ClipOutcome]:
        return [outcome for outcome in self.outcomes if outcome.status == STATUS_FAILED]


# --------------------------------------------------------------------------- #
# Manifesto
# --------------------------------------------------------------------------- #
def manifest_path(settings: Settings, actor_id: str) -> Path:
    return settings.actor_out_dir(actor_id) / MANIFEST_NAME


def load_manifest(settings: Settings, actor_id: str) -> Dict[str, object]:
    path = manifest_path(settings, actor_id)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("clips", {})
                return data
        except (json.JSONDecodeError, OSError) as exc:
            LOG.warning("Manifesto de %s ilegível (%s). Recriando.", actor_id, exc)

    actor = prompts_data.get_actor(actor_id)
    return {
        "schema_version": MANIFEST_SCHEMA,
        "actor": {
            "id": actor_id,
            "rotulo": actor["rotulo"],
            "descricao": actor["descricao"],
            "cenario": actor["cenario_curto"],
            "vieses_clinicos": actor["vieses_clinicos"],
        },
        "spec": {
            "container": "mp4",
            "video_codec": "h264",
            "profile": "main",
            "pix_fmt": "yuv420p",
            "resolution": f"{settings.out_size}x{settings.out_size}",
            "aspect_ratio": "1:1",
            "fps": settings.out_fps,
            "audio": None,
            "loop": (
                f"crossfade {settings.crossfade_seconds}s entre "
                f"[{settings.segment_seconds},{settings.segment_seconds * 2}) e "
                f"[0,{settings.segment_seconds})"
            ),
            "max_mp4_kb": settings.max_mp4_kb,
            "max_poster_kb": settings.max_poster_kb,
        },
        "clips": {},
        "created_at": iso_timestamp(settings.timezone),
        "updated_at": iso_timestamp(settings.timezone),
    }


def save_manifest(settings: Settings, actor_id: str, manifest: Dict[str, object]) -> Path:
    manifest["updated_at"] = iso_timestamp(settings.timezone)
    path = manifest_path(settings, actor_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def prompt_fingerprint(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def already_done(
    settings: Settings, manifest: Dict[str, object], actor_id: str, state_id: str, prompt: str
) -> Optional[Dict[str, object]]:
    """Retorna a entrada do manifesto se o clipe já estiver pronto e válido."""
    if settings.force:
        return None
    entry = (manifest.get("clips") or {}).get(state_id)
    if not isinstance(entry, dict):
        return None
    if entry.get("prompt_sha256") != prompt_fingerprint(prompt):
        LOG.debug("%s/%s: prompt mudou — regerando.", actor_id, state_id)
        return None
    out_dir = settings.actor_out_dir(actor_id)
    video = entry.get("video") or {}
    poster = entry.get("poster") or {}
    for meta in (video, poster):
        name = meta.get("file")
        if not name or not (out_dir / str(name)).is_file():
            return None
    return entry


# --------------------------------------------------------------------------- #
# Processamento de um clipe
# --------------------------------------------------------------------------- #
def process_one(
    settings: Settings,
    actor_id: str,
    state_id: str,
    *,
    index: int,
    generator: Optional[VeoGenerator],
    drive,
    drive_folder: str,
    tracker: BaseTracker,
    manifest: Dict[str, object],
    progress: Progress,
) -> ClipOutcome:
    label = f"{actor_id}/{state_id}"
    prompt = prompts_data.build_prompt(actor_id, state_id)

    # ---- 0. Retomada -----------------------------------------------------
    cached = already_done(settings, manifest, actor_id, state_id, prompt)
    if cached is not None:
        video = cached.get("video") or {}
        poster = cached.get("poster") or {}
        LOG.info("%s: já concluído — pulando (use --force para regerar).", label)
        tracker.update(
            actor_id,
            state_id,
            # `None` preserva a célula existente — reaproveitar um clipe nunca
            # deve apagar um link que já estava na planilha.
            RowUpdate(
                status=STATUS_SKIPPED,
                mp4_kb=video.get("kb"),
                video_link=video.get("web_view_link") or None,
                poster_link=poster.get("web_view_link") or None,
                sha256=video.get("sha256") or None,
                finished_at=str(cached.get("completed_at")) if cached.get("completed_at") else None,
            ),
        )
        return ClipOutcome(
            actor_id=actor_id,
            state_id=state_id,
            status=STATUS_SKIPPED,
            mp4_kb=video.get("kb"),
            video_link=str(video.get("web_view_link") or ""),
            poster_link=str(poster.get("web_view_link") or ""),
            sha256=str(video.get("sha256") or ""),
        )

    tracker.update(actor_id, state_id, RowUpdate(status=STATUS_RUNNING))

    raw_path = settings.raw_dir / f"{actor_id}_{state_id}_raw.mp4"
    out_dir = settings.actor_out_dir(actor_id)
    mp4_path = out_dir / f"{state_id}.mp4"
    jpg_path = out_dir / f"{state_id}.jpg"

    # ---- 1. Geração ------------------------------------------------------
    progress.set_stage(f"{label} · gerando")
    if settings.dry_run:
        LOG.info("%s: --dry-run — sintetizando clipe local (Veo não é chamado).", label)
        synth_raw_clip(settings, raw_path, seed=index)
        gen_meta = {
            "model": "(dry-run)",
            "aspect_ratio": "16:9",
            "duration_seconds": settings.veo_duration_seconds,
            "resolution": "720p",
        }
    else:
        assert generator is not None
        result = generator.generate(
            prompt, prompts_data.NEGATIVE_PROMPT, raw_path, label=label
        )
        gen_meta = {
            "model": result.model,
            "aspect_ratio": result.aspect_ratio,
            "duration_seconds": result.duration_seconds,
            "resolution": result.resolution,
            "poll_seconds": result.poll_seconds,
        }

    # ---- 2. Pós-processamento -------------------------------------------
    progress.set_stage(f"{label} · ffmpeg")
    processed: ProcessResult = process_clip(settings, raw_path, mp4_path, jpg_path)
    LOG.info(
        "%s: %s %.1f KB (%.2fs, crf %d) · pôster %.1f KB (q%d)",
        label, mp4_path.name, processed.mp4_kb, processed.duration_seconds,
        processed.crf, processed.jpg_kb, processed.poster_q,
    )

    # ---- 3. Upload -------------------------------------------------------
    progress.set_stage(f"{label} · upload")
    video_upload: Optional[UploadedFile] = None
    poster_upload: Optional[UploadedFile] = None
    if drive_folder or isinstance(drive, NullDriveManager):
        video_upload = drive.upload(mp4_path, drive_folder)
        poster_upload = drive.upload(jpg_path, drive_folder)

    # ---- 4. Manifesto ----------------------------------------------------
    clips = manifest.setdefault("clips", {})
    previous = clips.get(state_id) or {}
    state_meta = prompts_data.get_state(state_id)
    completed_at = timestamp(settings.timezone)

    clips[state_id] = {
        "state": state_id,
        "rotulo": state_meta["rotulo"],
        "descricao": state_meta["descricao"],
        "version": int(previous.get("version") or 0) + 1,
        "completed_at": completed_at,
        "prompt_sha256": prompt_fingerprint(prompt),
        "generation": gen_meta,
        "video": {
            "file": mp4_path.name,
            "sha256": processed.mp4_sha256,
            "bytes": processed.mp4_bytes,
            "kb": processed.mp4_kb,
            "duration_seconds": processed.duration_seconds,
            "width": processed.width,
            "height": processed.height,
            "fps": processed.fps,
            "crf": processed.crf,
            "audio": False,
            **({"file_id": video_upload.file_id, "web_view_link": video_upload.web_view_link}
               if video_upload and video_upload.file_id else {}),
        },
        "poster": {
            "file": jpg_path.name,
            "sha256": processed.jpg_sha256,
            "bytes": processed.jpg_bytes,
            "kb": processed.jpg_kb,
            "quality": processed.poster_q,
            **({"file_id": poster_upload.file_id, "web_view_link": poster_upload.web_view_link}
               if poster_upload and poster_upload.file_id else {}),
        },
    }
    local_manifest = save_manifest(settings, actor_id, manifest)

    if drive_folder or isinstance(drive, NullDriveManager):
        drive.upload(local_manifest, drive_folder, name=MANIFEST_NAME)

    # ---- 5. Planilha -----------------------------------------------------
    video_link = (
        video_upload.web_view_link if video_upload and video_upload.file_id
        else f"(local) {mp4_path}"
    )
    poster_link = (
        poster_upload.web_view_link if poster_upload and poster_upload.file_id
        else f"(local) {jpg_path}"
    )
    tracker.update(
        actor_id,
        state_id,
        RowUpdate(
            status=STATUS_DONE,
            mp4_kb=processed.mp4_kb,
            video_link=video_link,
            poster_link=poster_link,
            sha256=processed.mp4_sha256,
            finished_at=completed_at,
        ),
    )

    return ClipOutcome(
        actor_id=actor_id,
        state_id=state_id,
        status=STATUS_DONE,
        mp4_kb=processed.mp4_kb,
        video_link=video_link,
        poster_link=poster_link,
        sha256=processed.mp4_sha256,
    )


# --------------------------------------------------------------------------- #
# Orquestração
# --------------------------------------------------------------------------- #
def run(settings: Settings) -> RunSummary:
    settings.ensure_dirs()
    ensure_ffmpeg(settings)

    actors = settings.actors or prompts_data.ACTOR_ORDER
    states = settings.states or prompts_data.STATE_ORDER
    pairs: List[Tuple[str, str]] = list(prompts_data.iter_matrix(actors, states))

    _banner(settings, actors, states, len(pairs))

    tracker = build_tracker(settings)
    tracker.bootstrap(pairs)
    LOG.info("Planilha de controle: %s", tracker.describe())

    drive = DriveManager(settings) if settings.drive_enabled else NullDriveManager()
    folders: Dict[str, str] = {}
    if settings.drive_enabled:
        LOG.info("Drive autenticado como: %s", drive.whoami())
        folders = drive.ensure_structure(actors)
    else:
        LOG.warning("Upload para o Google Drive desabilitado (--skip-drive).")
        folders = {actor_id: "" for actor_id in actors}

    generator = VeoGenerator(settings) if not settings.dry_run else None

    summary = RunSummary()
    progress = Progress(len(pairs), desc="Ectoscopia")
    index = 0

    try:
        for actor_id in actors:
            actor = prompts_data.get_actor(actor_id)
            progress.write(f"\n▶ {actor['rotulo']}  ({actor['cenario_curto']})")
            manifest = load_manifest(settings, actor_id)

            for state_id in states:
                index += 1
                label = f"{actor_id}/{state_id}"
                try:
                    outcome = process_one(
                        settings,
                        actor_id,
                        state_id,
                        index=index,
                        generator=generator,
                        drive=drive,
                        drive_folder=folders.get(actor_id, ""),
                        tracker=tracker,
                        manifest=manifest,
                        progress=progress,
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # noqa: BLE001 - o pipeline não pode morrer aqui
                    reason = f"{type(exc).__name__}: {exc}"
                    LOG.error("%s: FALHA — %s", label, reason)
                    LOG.debug("%s", traceback.format_exc())
                    tracker.update(
                        actor_id,
                        state_id,
                        RowUpdate(
                            status=STATUS_FAILED,
                            note=reason[:400],
                            finished_at=timestamp(settings.timezone),
                        ),
                    )
                    outcome = ClipOutcome(
                        actor_id=actor_id, state_id=state_id, status=STATUS_FAILED, error=reason
                    )
                    summary.outcomes.append(outcome)
                    progress.advance(f"{label} ✗")
                    if settings.fail_fast:
                        raise
                    continue

                summary.outcomes.append(outcome)
                mark = "✓" if outcome.status == STATUS_DONE else "↩"
                progress.advance(f"{label} {mark}")
    except KeyboardInterrupt:
        progress.close()
        LOG.warning("Interrompido pelo usuário. Progresso preservado — basta reexecutar.")
        return summary
    finally:
        progress.close()
        tracker.close()

    _print_summary(settings, summary)
    return summary


def _banner(settings: Settings, actors: Sequence[str], states: Sequence[str], total: int) -> None:
    mode = []
    if settings.dry_run:
        mode.append("DRY-RUN (sem Veo)")
    if not settings.drive_enabled:
        mode.append("sem Drive")
    if settings.force:
        mode.append("force")

    print("\n" + "═" * 78, file=sys.stderr)
    print("  PIPELINE DE ECTOSCOPIA CLÍNICA — Veo 3.1 → FFmpeg → Drive → Planilha", file=sys.stderr)
    print("═" * 78, file=sys.stderr)
    print(f"  Personas ......... {', '.join(actors)}", file=sys.stderr)
    print(f"  Estados .......... {', '.join(states)}", file=sys.stderr)
    print(f"  Clipes ........... {total}", file=sys.stderr)
    print(f"  Modelo Veo ....... {settings.veo_model}", file=sys.stderr)
    print(
        f"  Saída ............ {settings.out_size}×{settings.out_size} @ {settings.out_fps}fps, "
        f"H.264 Main, sem áudio, ≤{settings.max_mp4_kb} KB",
        file=sys.stderr,
    )
    print(f"  Diretório ........ {settings.work_dir.resolve()}", file=sys.stderr)
    if mode:
        print(f"  Modo ............. {' · '.join(mode)}", file=sys.stderr)
    print("═" * 78 + "\n", file=sys.stderr)


def _print_summary(settings: Settings, summary: RunSummary) -> None:
    done = summary.count(STATUS_DONE)
    skipped = summary.count(STATUS_SKIPPED)
    failed = summary.count(STATUS_FAILED)

    print("\n" + "═" * 78, file=sys.stderr)
    print("  RESUMO DA EXECUÇÃO", file=sys.stderr)
    print("═" * 78, file=sys.stderr)
    print(f"  ✅ Concluídos ..... {done}", file=sys.stderr)
    print(f"  ↩️  Reaproveitados . {skipped}", file=sys.stderr)
    print(f"  ❌ Falhas ......... {failed}", file=sys.stderr)

    if summary.failures:
        print("\n  Falhas detalhadas:", file=sys.stderr)
        for outcome in summary.failures:
            print(f"    · {outcome.actor_id}/{outcome.state_id} — {outcome.error}", file=sys.stderr)
        print("\n  Reexecute o comando: clipes prontos são pulados automaticamente.", file=sys.stderr)

    sizes = [o.mp4_kb for o in summary.outcomes if o.mp4_kb]
    if sizes:
        print(
            f"\n  MP4: min {min(sizes):.1f} KB · média {sum(sizes) / len(sizes):.1f} KB "
            f"· máx {max(sizes):.1f} KB (teto {settings.max_mp4_kb} KB)",
            file=sys.stderr,
        )
    print(f"  Artefatos locais: {settings.out_dir.resolve()}", file=sys.stderr)
    if settings.tracker_backend == "excel":
        print(f"  Planilha: {Path(settings.excel_path).resolve()}", file=sys.stderr)
    print("═" * 78 + "\n", file=sys.stderr)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_pipeline.py",
        description="Gera, processa, publica e rastreia os 20 clipes de ectoscopia clínica.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemplos:\n"
            "  python run_pipeline.py --dry-run --skip-drive\n"
            "  python run_pipeline.py --actors A01 A03\n"
            "  python run_pipeline.py --states basal dispneia --force\n"
            "  python run_pipeline.py --tracker sheets\n"
        ),
    )
    parser.add_argument(
        "--actors", nargs="+", choices=prompts_data.ACTOR_ORDER, metavar="A0x",
        help="Personas a processar (padrão: todas).",
    )
    parser.add_argument(
        "--states", nargs="+", choices=prompts_data.STATE_ORDER, metavar="ESTADO",
        help="Estados a processar (padrão: todos).",
    )
    parser.add_argument("--work-dir", type=Path, help="Diretório de trabalho (padrão: build/).")
    parser.add_argument(
        "--tracker", choices=["excel", "sheets", "none"], help="Backend da planilha."
    )
    parser.add_argument("--excel-path", help="Caminho do .xlsx de controle.")
    parser.add_argument("--model", help="Sobrescreve o modelo do Veo.")
    parser.add_argument(
        "--skip-drive", action="store_true", help="Não autenticar nem subir nada no Drive."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Não chama o Veo: sintetiza clipes locais para validar todo o resto.",
    )
    parser.add_argument(
        "--force", action="store_true", help="Regera mesmo os clipes já concluídos."
    )
    parser.add_argument(
        "--fail-fast", action="store_true", help="Aborta na primeira falha (padrão: continua)."
    )
    parser.add_argument(
        "--print-prompt", nargs=2, metavar=("ATOR", "ESTADO"),
        help="Imprime o prompt composto e sai (útil para revisão clínica).",
    )
    parser.add_argument(
        "--list", action="store_true", help="Lista a matriz ator × estado e sai."
    )
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    if args.list:
        for actor_id in prompts_data.ACTOR_ORDER:
            actor = prompts_data.get_actor(actor_id)
            print(f"\n{actor['rotulo']}")
            print(f"  {actor['descricao']}")
            for state_id in prompts_data.STATE_ORDER:
                state = prompts_data.get_state(state_id)
                print(f"    · {state_id:<18} {state['rotulo']} — {state['descricao']}")
        print(f"\nTotal: {prompts_data.matrix_size()} clipes.\n")
        return 0

    if args.print_prompt:
        actor_id, state_id = args.print_prompt
        print(prompts_data.build_prompt(actor_id, state_id))
        print("\n--- NEGATIVE PROMPT ---\n")
        print(prompts_data.NEGATIVE_PROMPT)
        return 0

    overrides = {
        "actors": args.actors,
        "states": args.states,
        "work_dir": args.work_dir,
        "tracker_backend": args.tracker,
        "excel_path": args.excel_path,
        "veo_model": args.model,
        "dry_run": args.dry_run or None,
        "force": args.force or None,
        "fail_fast": args.fail_fast or None,
        "log_level": args.log_level,
    }
    if args.skip_drive:
        overrides["drive_enabled"] = False

    settings = load_settings(overrides)
    setup_logging(settings.log_level, settings.log_dir / "pipeline.log")

    problems = settings.validate()
    if problems:
        LOG.error("Configuração inválida:")
        for problem in problems:
            LOG.error("  • %s", problem)
        LOG.error("Ajuste o .env (veja .env.example) ou use --dry-run/--skip-drive.")
        return 2

    try:
        summary = run(settings)
    except (VeoError, DriveError, FFmpegError, SizeBudgetError) as exc:
        LOG.error("Pipeline abortado: %s", exc)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130

    return 1 if summary.failures else 0


if __name__ == "__main__":
    sys.exit(main())
