"""Utilitários transversais: logging, retentativas com backoff e barra de progresso."""

from __future__ import annotations

import logging
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence, Tuple, Type, TypeVar

try:  # ZoneInfo é stdlib a partir do 3.9; tzdata pode faltar em imagens slim
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

try:
    from tqdm import tqdm as _tqdm
except Exception:  # pragma: no cover - tqdm é opcional
    _tqdm = None

T = TypeVar("T")
LOG = logging.getLogger("ectoscopia")


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
class _ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[38;5;245m",
        logging.INFO: "\033[38;5;39m",
        logging.WARNING: "\033[38;5;214m",
        logging.ERROR: "\033[38;5;203m",
        logging.CRITICAL: "\033[1;38;5;196m",
    }
    RESET = "\033[0m"

    def __init__(self, fmt: str, use_color: bool) -> None:
        super().__init__(fmt, datefmt="%H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self.use_color:
            return text
        color = self.COLORS.get(record.levelno, "")
        return f"{color}{text}{self.RESET}" if color else text


def setup_logging(level: str = "INFO", log_file: Optional[Path] = None) -> logging.Logger:
    """Configura o logger raiz do pipeline (console colorido + arquivo opcional)."""
    LOG.handlers.clear()
    LOG.setLevel(getattr(logging, level.upper(), logging.INFO))
    LOG.propagate = False

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(
        _ColorFormatter("%(asctime)s │ %(levelname)-7s │ %(message)s", sys.stderr.isatty())
    )
    LOG.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s")
        )
        file_handler.setLevel(logging.DEBUG)
        LOG.addHandler(file_handler)

    # Bibliotecas do Google são verbosas demais em DEBUG.
    for noisy in ("googleapiclient", "google_auth_httplib2", "urllib3", "google"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return LOG


# --------------------------------------------------------------------------- #
# Retentativas
# --------------------------------------------------------------------------- #
RATE_LIMIT_MARKERS: Tuple[str, ...] = (
    "429",
    "resource_exhausted",
    "resource exhausted",
    "rate limit",
    "ratelimit",
    "quota",
    "too many requests",
    "503",
    "500",
    "502",
    "504",
    "unavailable",
    "deadline",
    "timeout",
    "connection reset",
    "temporarily",
    "internal error",
)


def is_retryable(exc: BaseException) -> bool:
    """Heurística de erro transitório (rate limit, indisponibilidade, rede)."""
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int) and (status == 429 or 500 <= status < 600):
        return True
    resp = getattr(exc, "resp", None)  # googleapiclient.errors.HttpError
    if resp is not None:
        resp_status = getattr(resp, "status", None)
        if isinstance(resp_status, int) and (resp_status == 429 or 500 <= resp_status < 600):
            return True
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in RATE_LIMIT_MARKERS)


def retry(
    func: Callable[[], T],
    *,
    what: str,
    attempts: int = 5,
    base_delay: float = 5.0,
    max_delay: float = 300.0,
    retryable: Callable[[BaseException], bool] = is_retryable,
    exceptions: Sequence[Type[BaseException]] = (Exception,),
) -> T:
    """Executa `func` com backoff exponencial + jitter em erros transitórios."""
    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except tuple(exceptions) as exc:  # noqa: PERF203 - retentativa é o ponto
            last_exc = exc
            if attempt >= attempts or not retryable(exc):
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay += random.uniform(0, delay * 0.25)  # jitter anti-thundering-herd
            LOG.warning(
                "%s falhou (tentativa %d/%d): %s — nova tentativa em %.1fs",
                what,
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)
    assert last_exc is not None  # pragma: no cover - inalcançável
    raise last_exc


# --------------------------------------------------------------------------- #
# Formatação e tempo
# --------------------------------------------------------------------------- #
def kb(num_bytes: int) -> float:
    return round(num_bytes / 1024.0, 1)


def human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"  # pragma: no cover


def now_local(tz_name: str = "America/Sao_Paulo") -> datetime:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except Exception:
            pass
    return datetime.now(timezone.utc)


def timestamp(tz_name: str = "America/Sao_Paulo") -> str:
    return now_local(tz_name).strftime("%Y-%m-%d %H:%M:%S")


def iso_timestamp(tz_name: str = "America/Sao_Paulo") -> str:
    return now_local(tz_name).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Barra de progresso (tqdm quando disponível, fallback stdlib caso contrário)
# --------------------------------------------------------------------------- #
class Progress:
    """Barra de progresso com fallback puro-stdlib, segura para pipes/CI."""

    def __init__(self, total: int, desc: str = "Progresso") -> None:
        self.total = max(total, 1)
        self.desc = desc
        self.count = 0
        self.start = time.monotonic()
        self._bar = None
        if _tqdm is not None and sys.stderr.isatty():
            self._bar = _tqdm(
                total=self.total,
                desc=desc,
                unit="clipe",
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]",
                leave=True,
            )

    def set_stage(self, text: str) -> None:
        if self._bar is not None:
            self._bar.set_postfix_str(text[:48], refresh=True)
        else:
            # Sem TTY (pipe, CI, nohup) a barra viva só polui o log: o estágio
            # vira uma linha de debug e o progresso aparece só nos avanços.
            LOG.debug("etapa: %s", text)

    def advance(self, text: str = "") -> None:
        self.count += 1
        if self._bar is not None:
            if text:
                self._bar.set_postfix_str(text[:48], refresh=False)
            self._bar.update(1)
        else:
            pct = 100.0 * self.count / self.total
            filled = int(28 * self.count / self.total)
            bar = "█" * filled + "░" * (28 - filled)
            elapsed = time.monotonic() - self.start
            terminator = "\n" if not sys.stderr.isatty() or self.count >= self.total else ""
            sys.stderr.write(
                f"\r{self.desc}: |{bar}| {self.count}/{self.total} "
                f"({pct:5.1f}%) {elapsed:6.1f}s {text[:40]}{terminator}"
            )
            sys.stderr.flush()

    def write(self, message: str) -> None:
        """Imprime sem quebrar a barra."""
        if self._bar is not None:
            self._bar.write(message)
        else:
            if sys.stderr.isatty():
                sys.stderr.write("\r" + " " * 110 + "\r")
            sys.stderr.write(message + "\n")
            sys.stderr.flush()

    def close(self) -> None:
        if self._bar is not None:
            self._bar.close()


def chunked(seq: Iterable[T], size: int) -> Iterable[list]:
    batch: list = []
    for item in seq:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
