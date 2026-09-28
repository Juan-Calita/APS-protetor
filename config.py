"""Configuração central do pipeline de ectoscopia clínica.

Todos os parâmetros vêm de variáveis de ambiente (ou de um arquivo `.env`),
com defaults sãos para que o pipeline rode com o mínimo de cerimônia.
Veja `.env.example` para o inventário completo.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

try:  # opcional, mas muito conveniente
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv é opcional
    pass


# --------------------------------------------------------------------------- #
# Helpers de leitura de ambiente
# --------------------------------------------------------------------------- #
def _env_str(key: str, default: str = "") -> str:
    value = os.environ.get(key)
    return default if value is None or value == "" else value


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env_str(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env_str(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = _env_str(key, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "y", "on", "sim", "s"}


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    """Snapshot imutável-por-convenção da configuração de uma execução."""

    # ---- Veo / google-genai -------------------------------------------------
    google_api_key: str = field(default_factory=lambda: _env_str("GOOGLE_API_KEY"))
    veo_model: str = field(
        default_factory=lambda: _env_str("VEO_MODEL", "veo-3.1-fast-generate-preview")
    )
    veo_aspect_ratio: str = field(default_factory=lambda: _env_str("VEO_ASPECT_RATIO", "16:9"))
    veo_resolution: str = field(default_factory=lambda: _env_str("VEO_RESOLUTION", "720p"))
    veo_duration_seconds: int = field(default_factory=lambda: _env_int("VEO_DURATION_SECONDS", 6))
    veo_person_generation: str = field(
        default_factory=lambda: _env_str("VEO_PERSON_GENERATION", "allow_adult")
    )
    veo_poll_interval: float = field(default_factory=lambda: _env_float("VEO_POLL_INTERVAL", 10.0))
    veo_poll_timeout: float = field(default_factory=lambda: _env_float("VEO_POLL_TIMEOUT", 900.0))
    veo_max_attempts: int = field(default_factory=lambda: _env_int("VEO_MAX_ATTEMPTS", 5))
    veo_backoff_base: float = field(default_factory=lambda: _env_float("VEO_BACKOFF_BASE", 8.0))
    veo_backoff_max: float = field(default_factory=lambda: _env_float("VEO_BACKOFF_MAX", 300.0))
    veo_cooldown_between_calls: float = field(
        default_factory=lambda: _env_float("VEO_COOLDOWN_BETWEEN_CALLS", 3.0)
    )

    # ---- FFmpeg / pós-processamento ----------------------------------------
    ffmpeg_bin: str = field(default_factory=lambda: _env_str("FFMPEG_BIN", "ffmpeg"))
    ffprobe_bin: str = field(default_factory=lambda: _env_str("FFPROBE_BIN", "ffprobe"))
    out_size: int = field(default_factory=lambda: _env_int("OUT_SIZE", 720))
    out_fps: int = field(default_factory=lambda: _env_int("OUT_FPS", 24))
    segment_seconds: float = field(default_factory=lambda: _env_float("SEGMENT_SECONDS", 2.0))
    crossfade_seconds: float = field(default_factory=lambda: _env_float("CROSSFADE_SECONDS", 0.5))
    crf_start: int = field(default_factory=lambda: _env_int("CRF_START", 27))
    crf_max: int = field(default_factory=lambda: _env_int("CRF_MAX", 40))
    crf_step: int = field(default_factory=lambda: _env_int("CRF_STEP", 3))
    x264_preset: str = field(default_factory=lambda: _env_str("X264_PRESET", "slow"))
    max_mp4_kb: int = field(default_factory=lambda: _env_int("MAX_MP4_KB", 600))
    poster_q_start: int = field(default_factory=lambda: _env_int("POSTER_Q_START", 4))
    poster_q_max: int = field(default_factory=lambda: _env_int("POSTER_Q_MAX", 12))
    max_poster_kb: int = field(default_factory=lambda: _env_int("MAX_POSTER_KB", 60))

    # ---- Google Drive -------------------------------------------------------
    drive_enabled: bool = field(default_factory=lambda: _env_bool("DRIVE_ENABLED", True))
    drive_auth_mode: str = field(default_factory=lambda: _env_str("DRIVE_AUTH_MODE", "service_account"))
    drive_service_account_file: str = field(
        default_factory=lambda: _env_str("DRIVE_SERVICE_ACCOUNT_FILE", "service_account.json")
    )
    drive_oauth_client_file: str = field(
        default_factory=lambda: _env_str("DRIVE_OAUTH_CLIENT_FILE", "client_secret.json")
    )
    drive_oauth_token_file: str = field(
        default_factory=lambda: _env_str("DRIVE_OAUTH_TOKEN_FILE", "drive_token.json")
    )
    drive_root_path: str = field(
        default_factory=lambda: _env_str("DRIVE_ROOT_PATH", "medical-assets/ectoscopia")
    )
    # Pasta-pai já existente (Meu Drive compartilhado com a Service Account, ou
    # a raiz de um Shared Drive). Obrigatório para Service Accounts, que não
    # possuem cota de armazenamento própria.
    drive_root_parent_id: str = field(default_factory=lambda: _env_str("DRIVE_ROOT_PARENT_ID"))
    drive_shared_drive_id: str = field(default_factory=lambda: _env_str("DRIVE_SHARED_DRIVE_ID"))
    drive_make_public: bool = field(default_factory=lambda: _env_bool("DRIVE_MAKE_PUBLIC", False))
    drive_max_attempts: int = field(default_factory=lambda: _env_int("DRIVE_MAX_ATTEMPTS", 5))

    # ---- Planilha de controle ----------------------------------------------
    tracker_backend: str = field(default_factory=lambda: _env_str("TRACKER_BACKEND", "excel"))
    excel_path: str = field(
        default_factory=lambda: _env_str("EXCEL_PATH", "controle_producao_ectoscopia.xlsx")
    )
    sheets_spreadsheet_id: str = field(default_factory=lambda: _env_str("SHEETS_SPREADSHEET_ID"))
    sheets_worksheet: str = field(default_factory=lambda: _env_str("SHEETS_WORKSHEET", "Producao"))
    sheets_credentials_file: str = field(
        default_factory=lambda: _env_str("SHEETS_CREDENTIALS_FILE", "service_account.json")
    )

    # ---- Execução -----------------------------------------------------------
    work_dir: Path = field(default_factory=lambda: Path(_env_str("WORK_DIR", "build")))
    actors: List[str] = field(default_factory=list)
    states: List[str] = field(default_factory=list)
    force: bool = False
    dry_run: bool = False
    fail_fast: bool = False
    timezone: str = field(default_factory=lambda: _env_str("TIMEZONE", "America/Sao_Paulo"))
    log_level: str = field(default_factory=lambda: _env_str("LOG_LEVEL", "INFO"))

    # ---- Caminhos derivados -------------------------------------------------
    @property
    def raw_dir(self) -> Path:
        return self.work_dir / "raw"

    @property
    def out_dir(self) -> Path:
        return self.work_dir / "out"

    @property
    def log_dir(self) -> Path:
        return self.work_dir / "logs"

    def actor_out_dir(self, actor_id: str) -> Path:
        return self.out_dir / actor_id

    def ensure_dirs(self) -> None:
        for path in (self.work_dir, self.raw_dir, self.out_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)

    # ---- Validação ----------------------------------------------------------
    def validate(self) -> List[str]:
        """Retorna a lista de problemas de configuração (vazia = tudo certo)."""
        problems: List[str] = []

        if not self.dry_run and not self.google_api_key:
            problems.append(
                "GOOGLE_API_KEY não definida — necessária para chamar o Veo "
                "(use --dry-run para testar o pipeline sem gerar vídeos)."
            )

        if self.drive_enabled:
            if self.drive_auth_mode not in {"service_account", "oauth"}:
                problems.append(
                    f"DRIVE_AUTH_MODE inválido: {self.drive_auth_mode!r} "
                    "(use 'service_account' ou 'oauth')."
                )
            if self.drive_auth_mode == "service_account":
                if not Path(self.drive_service_account_file).is_file():
                    problems.append(
                        f"Credencial de Service Account não encontrada: "
                        f"{self.drive_service_account_file}"
                    )
                if not self.drive_root_parent_id and not self.drive_shared_drive_id:
                    problems.append(
                        "Service Accounts não possuem cota de armazenamento no Drive. "
                        "Defina DRIVE_ROOT_PARENT_ID (pasta do Meu Drive compartilhada "
                        "com a SA) ou DRIVE_SHARED_DRIVE_ID (Drive compartilhado)."
                    )
            elif not Path(self.drive_oauth_client_file).is_file():
                problems.append(
                    f"Client secret OAuth não encontrado: {self.drive_oauth_client_file}"
                )

        if self.tracker_backend not in {"excel", "sheets", "none"}:
            problems.append(
                f"TRACKER_BACKEND inválido: {self.tracker_backend!r} "
                "(use 'excel', 'sheets' ou 'none')."
            )
        if self.tracker_backend == "sheets" and not self.sheets_spreadsheet_id:
            problems.append("TRACKER_BACKEND=sheets exige SHEETS_SPREADSHEET_ID.")

        if self.crossfade_seconds >= self.segment_seconds:
            problems.append(
                "CROSSFADE_SECONDS deve ser menor que SEGMENT_SECONDS "
                f"(atual: {self.crossfade_seconds} >= {self.segment_seconds})."
            )
        if self.out_size % 2 != 0:
            problems.append("OUT_SIZE deve ser par (requisito do yuv420p).")

        return problems


def load_settings(overrides: Optional[dict] = None) -> Settings:
    """Cria um `Settings` a partir do ambiente, aplicando overrides do CLI."""
    settings = Settings()
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        if not hasattr(settings, key):
            raise AttributeError(f"Override desconhecido para Settings: {key}")
        setattr(settings, key, value)
    return settings
