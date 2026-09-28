"""Integração com o Google Veo 3.1 via SDK `google-genai`.

Fluxo: dispara a operação assíncrona → faz polling com backoff até
``operation.done`` → baixa o MP4 bruto para disco.

Notas de campo (importantes)
----------------------------
* **Proporção.** O Veo 3.x aceita apenas ``16:9`` e ``9:16``. Não existe
  ``1:1`` nativo. Pedimos 16:9 em 720p e o `processor` faz o *center crop*
  para 720×720 — o resultado final cumpre o requisito de 1:1 sem distorção.
  Se um dia a API passar a aceitar ``1:1``, basta definir ``VEO_ASPECT_RATIO=1:1``:
  o fallback automático abaixo cuida do resto.
* **Duração.** O Veo 3.1 trabalha com durações discretas (tipicamente 4, 6 e 8 s).
  Pedimos ``VEO_DURATION_SECONDS`` (default 6) e, se a API recusar, descemos
  por uma escada de fallback. Qualquer valor ≥ 4 s satisfaz o corte
  ``[2s,4s) + [0s,2s)`` do pós-processamento.
* **Áudio.** O Veo 3.x gera áudio nativo. Ele é descartado de forma dura pelo
  ``-an`` no encode final, então nenhuma faixa sobrevive ao pipeline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import Settings
from utils import LOG, human_size, is_retryable, retry


class VeoError(RuntimeError):
    """Falha irrecuperável na geração de um clipe."""


# Escada de fallback quando a API recusa o parâmetro pedido.
ASPECT_FALLBACKS: List[str] = ["16:9", "9:16"]
DURATION_FALLBACKS: List[int] = [6, 8, 4]

_UNSUPPORTED_MARKERS = (
    "invalid_argument",
    "invalid argument",
    "not supported",
    "unsupported",
    "must be one of",
    "allowed values",
    "aspect_ratio",
    "duration_seconds",
    "400",
)


def _looks_unsupported(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _UNSUPPORTED_MARKERS)


def _rejected_parameter(exc: BaseException) -> Optional[str]:
    """Identifica QUAL parâmetro a API recusou, para não queimar cota à toa.

    Sem isso, um ``aspect_ratio`` inválido faria o código testar todas as
    durações com o mesmo valor errado — três chamadas desperdiçadas por clipe.
    """
    text = f"{exc}".lower()
    if "aspect" in text:
        return "aspect_ratio"
    if "duration" in text or "length" in text:
        return "duration_seconds"
    return None


@dataclass
class GenerationResult:
    raw_path: Path
    model: str
    aspect_ratio: str
    duration_seconds: int
    resolution: str
    poll_seconds: float
    attempts: int


class VeoGenerator:
    """Wrapper fino e resiliente sobre ``client.models.generate_videos``."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None
        self._types = None
        self._aspect_ratio = settings.veo_aspect_ratio
        self._duration = settings.veo_duration_seconds
        self._last_call_at: float = 0.0

    # ------------------------------------------------------------------ #
    # Cliente
    # ------------------------------------------------------------------ #
    @property
    def client(self):
        if self._client is None:
            try:
                from google import genai
                from google.genai import types
            except ImportError as exc:  # pragma: no cover
                raise VeoError(
                    "Pacote `google-genai` não instalado. Rode: pip install -r requirements.txt"
                ) from exc
            if not self.settings.google_api_key:
                raise VeoError("GOOGLE_API_KEY não definida.")
            self._client = genai.Client(api_key=self.settings.google_api_key)
            self._types = types
            LOG.info("Cliente google-genai inicializado (modelo %s).", self.settings.veo_model)
        return self._client

    @property
    def types(self):
        if self._types is None:
            _ = self.client  # força a inicialização
        return self._types

    # ------------------------------------------------------------------ #
    # Config de geração
    # ------------------------------------------------------------------ #
    def _build_config(self, negative_prompt: str, aspect_ratio: str, duration: int):
        kwargs: Dict[str, Any] = {
            "aspect_ratio": aspect_ratio,
            "number_of_videos": 1,
            "duration_seconds": duration,
            "negative_prompt": negative_prompt,
            "person_generation": self.settings.veo_person_generation,
            "resolution": self.settings.veo_resolution,
        }
        # O SDK evolui rápido: descarta silenciosamente campos que a versão
        # instalada não conhece, em vez de explodir na cara do usuário.
        config_cls = self.types.GenerateVideosConfig
        try:
            valid = set(getattr(config_cls, "model_fields", {}) or {})
        except Exception:  # pragma: no cover
            valid = set()
        if valid:
            dropped = [key for key in kwargs if key not in valid]
            for key in dropped:
                kwargs.pop(key)
            if dropped:
                LOG.debug("Campos ignorados por esta versão do SDK: %s", ", ".join(dropped))
        return config_cls(**kwargs)

    def _throttle(self) -> None:
        """Espaça chamadas consecutivas para não provocar rate limit de saída."""
        cooldown = self.settings.veo_cooldown_between_calls
        if cooldown <= 0:
            return
        elapsed = time.monotonic() - self._last_call_at
        if self._last_call_at and elapsed < cooldown:
            time.sleep(cooldown - elapsed)

    # ------------------------------------------------------------------ #
    # Polling
    # ------------------------------------------------------------------ #
    def _await_operation(self, operation, label: str):
        deadline = time.monotonic() + self.settings.veo_poll_timeout
        interval = self.settings.veo_poll_interval
        waited = 0.0

        while not getattr(operation, "done", False):
            if time.monotonic() > deadline:
                raise VeoError(
                    f"{label}: operação não concluiu em {self.settings.veo_poll_timeout:.0f}s "
                    "(VEO_POLL_TIMEOUT). O job pode seguir no servidor — tente novamente."
                )
            time.sleep(interval)
            waited += interval
            operation = retry(
                lambda: self.client.operations.get(operation),
                what=f"{label}: polling",
                attempts=self.settings.veo_max_attempts,
                base_delay=self.settings.veo_backoff_base,
                max_delay=self.settings.veo_backoff_max,
            )
            LOG.debug("%s: aguardando… %.0fs", label, waited)
            # Backoff suave no polling: operações longas não precisam de 10s fixos.
            interval = min(interval * 1.15, 30.0)

        error = getattr(operation, "error", None)
        if error:
            raise VeoError(f"{label}: o Veo retornou erro: {error}")
        return operation

    @staticmethod
    def _extract_video(operation, label: str):
        response = getattr(operation, "response", None) or getattr(operation, "result", None)
        videos = getattr(response, "generated_videos", None) if response else None
        if not videos:
            rai = getattr(response, "rai_media_filtered_reasons", None) if response else None
            if rai:
                raise VeoError(
                    f"{label}: geração bloqueada pelos filtros de segurança do Veo: {rai}. "
                    "Ajuste o prompt em prompts_data.py."
                )
            raise VeoError(f"{label}: resposta do Veo sem vídeos gerados.")
        return videos[0].video

    def _download(self, video, dst: Path, label: str) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)

        def _do() -> None:
            payload = getattr(video, "video_bytes", None)
            if not payload:
                # SDKs recentes exigem o download explícito antes do .save()
                self.client.files.download(file=video)
                payload = getattr(video, "video_bytes", None)
            if payload:
                dst.write_bytes(payload)
                return
            save = getattr(video, "save", None)
            if callable(save):
                save(str(dst))
                return
            raise VeoError(f"{label}: não foi possível materializar os bytes do vídeo.")

        retry(
            _do,
            what=f"{label}: download",
            attempts=self.settings.veo_max_attempts,
            base_delay=self.settings.veo_backoff_base,
            max_delay=self.settings.veo_backoff_max,
        )

        if not dst.is_file() or dst.stat().st_size == 0:
            raise VeoError(f"{label}: download resultou em arquivo vazio ({dst}).")

    # ------------------------------------------------------------------ #
    # API pública
    # ------------------------------------------------------------------ #
    def generate(
        self,
        prompt: str,
        negative_prompt: str,
        dst: Path,
        *,
        label: str = "clipe",
    ) -> GenerationResult:
        """Gera um clipe e salva o MP4 bruto em `dst`.

        Aplica retentativa com backoff em rate limit/erros transitórios e faz
        fallback automático de `aspect_ratio`/`duration_seconds` quando a API
        recusa o valor pedido.
        """
        started = time.monotonic()
        attempts_used = 0

        aspect_candidates = [self._aspect_ratio] + [
            value for value in ASPECT_FALLBACKS if value != self._aspect_ratio
        ]
        duration_candidates = [self._duration] + [
            value for value in DURATION_FALLBACKS if value != self._duration
        ]

        last_exc: Optional[BaseException] = None
        for aspect_ratio in aspect_candidates:
            for duration in duration_candidates:
                attempts_used += 1
                try:
                    self._throttle()
                    config = self._build_config(negative_prompt, aspect_ratio, duration)

                    def _start():
                        self._last_call_at = time.monotonic()
                        return self.client.models.generate_videos(
                            model=self.settings.veo_model,
                            prompt=prompt,
                            config=config,
                        )

                    LOG.info(
                        "%s: solicitando geração (%s, %ds, %s)…",
                        label, aspect_ratio, duration, self.settings.veo_resolution,
                    )
                    operation = retry(
                        _start,
                        what=f"{label}: generate_videos",
                        attempts=self.settings.veo_max_attempts,
                        base_delay=self.settings.veo_backoff_base,
                        max_delay=self.settings.veo_backoff_max,
                        retryable=lambda exc: is_retryable(exc) and not _looks_unsupported(exc),
                    )

                    operation = self._await_operation(operation, label)
                    video = self._extract_video(operation, label)
                    self._download(video, dst, label)

                    # Memoriza a combinação aceita para os próximos clipes.
                    self._aspect_ratio, self._duration = aspect_ratio, duration
                    elapsed = time.monotonic() - started
                    LOG.info(
                        "%s: bruto salvo (%s) em %.1fs.", label, human_size(dst.stat().st_size), elapsed
                    )
                    return GenerationResult(
                        raw_path=dst,
                        model=self.settings.veo_model,
                        aspect_ratio=aspect_ratio,
                        duration_seconds=duration,
                        resolution=self.settings.veo_resolution,
                        poll_seconds=round(elapsed, 1),
                        attempts=attempts_used,
                    )

                except Exception as exc:  # noqa: BLE001 - decidimos o destino abaixo
                    last_exc = exc
                    if not _looks_unsupported(exc):
                        raise VeoError(f"{label}: {exc}") from exc

                    rejected = _rejected_parameter(exc)
                    LOG.warning(
                        "%s: combinação %s/%ds recusada pela API (%s).",
                        label, aspect_ratio, duration, exc,
                    )
                    if rejected == "aspect_ratio":
                        # Trocar a duração não conserta um aspect ratio inválido:
                        # salta direto para o próximo aspect ratio.
                        break
                    continue

        raise VeoError(
            f"{label}: nenhuma combinação de aspect_ratio/duração foi aceita. "
            f"Último erro: {last_exc}"
        )
