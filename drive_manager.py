"""Criação de estrutura de pastas e upload de arquivos no Google Drive.

Estrutura produzida
-------------------
    <raiz>/medical-assets/ectoscopia/A01/{basal.mp4, basal.jpg, …, manifest.json}
                                    /A02/…
                                    /A03/…
                                    /A04/…

Autenticação
------------
* ``service_account`` (padrão) — headless, ideal para CI/cron.
* ``oauth``               — fluxo de navegador na primeira execução, token cacheado.

⚠️ Service Accounts **não possuem cota de armazenamento própria** no Drive.
Um upload para o "Meu Drive" da SA falha com ``storageQuotaExceeded``. Portanto
é obrigatório definir **uma** das opções:

* ``DRIVE_ROOT_PARENT_ID``  — id de uma pasta do seu Meu Drive compartilhada
  com o e-mail da Service Account, com permissão de Editor; ou
* ``DRIVE_SHARED_DRIVE_ID`` — id de um Drive compartilhado onde a SA é membro.

Idempotência
------------
Pastas e arquivos são procurados por nome antes de criados. Reexecutar o
pipeline **atualiza** o arquivo existente (nova revisão, mesmo id e mesmo
``webViewLink``) em vez de criar duplicatas.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from config import Settings
from utils import LOG, human_size, retry

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"

MIME_BY_SUFFIX = {
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".json": "application/json",
}


class DriveError(RuntimeError):
    """Falha de autenticação ou de operação no Drive."""


@dataclass
class UploadedFile:
    file_id: str
    name: str
    web_view_link: str
    size_bytes: int
    created: bool  # True = criado agora; False = revisão de arquivo existente

    def to_dict(self) -> Dict[str, object]:
        return {
            "file_id": self.file_id,
            "name": self.name,
            "web_view_link": self.web_view_link,
            "size_bytes": self.size_bytes,
        }


def _escape(value: str) -> str:
    """Escapa aspas simples para a sintaxe de query `q` do Drive."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DriveManager:
    """Fachada mínima e idempotente sobre a Drive API v3."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._service = None
        self._folder_cache: Dict[str, str] = {}
        self._root_id: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Autenticação
    # ------------------------------------------------------------------ #
    def _credentials(self):
        mode = self.settings.drive_auth_mode
        if mode == "service_account":
            try:
                from google.oauth2 import service_account
            except ImportError as exc:  # pragma: no cover
                raise DriveError("Instale `google-auth` (pip install -r requirements.txt).") from exc
            path = Path(self.settings.drive_service_account_file)
            if not path.is_file():
                raise DriveError(f"Credencial de Service Account não encontrada: {path}")
            return service_account.Credentials.from_service_account_file(str(path), scopes=SCOPES)

        if mode == "oauth":
            try:
                from google.auth.transport.requests import Request
                from google.oauth2.credentials import Credentials
                from google_auth_oauthlib.flow import InstalledAppFlow
            except ImportError as exc:  # pragma: no cover
                raise DriveError(
                    "Instale `google-auth-oauthlib` (pip install -r requirements.txt)."
                ) from exc

            token_path = Path(self.settings.drive_oauth_token_file)
            creds = None
            if token_path.is_file():
                creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            if not creds or not creds.valid:
                client_file = Path(self.settings.drive_oauth_client_file)
                if not client_file.is_file():
                    raise DriveError(f"Client secret OAuth não encontrado: {client_file}")
                flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
                creds = flow.run_local_server(port=0)
                token_path.write_text(creds.to_json(), encoding="utf-8")
                LOG.info("Token OAuth salvo em %s", token_path)
            return creds

        raise DriveError(f"DRIVE_AUTH_MODE inválido: {mode!r}")

    @property
    def service(self):
        if self._service is None:
            try:
                from googleapiclient.discovery import build
            except ImportError as exc:  # pragma: no cover
                raise DriveError(
                    "Instale `google-api-python-client` (pip install -r requirements.txt)."
                ) from exc
            self._service = build(
                "drive", "v3", credentials=self._credentials(), cache_discovery=False
            )
            LOG.info("Drive API autenticada (%s).", self.settings.drive_auth_mode)
        return self._service

    # ------------------------------------------------------------------ #
    # Parâmetros comuns
    # ------------------------------------------------------------------ #
    @property
    def _shared_drive_args(self) -> Dict[str, object]:
        args: Dict[str, object] = {"supportsAllDrives": True}
        return args

    def _list_args(self) -> Dict[str, object]:
        args: Dict[str, object] = {
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if self.settings.drive_shared_drive_id:
            args["corpora"] = "drive"
            args["driveId"] = self.settings.drive_shared_drive_id
        return args

    def _call(self, request, what: str):
        """Executa uma request da API com retentativa em 429/5xx."""
        return retry(
            request.execute,
            what=f"Drive: {what}",
            attempts=self.settings.drive_max_attempts,
            base_delay=4.0,
            max_delay=120.0,
        )

    # ------------------------------------------------------------------ #
    # Pastas
    # ------------------------------------------------------------------ #
    def _find_child(self, name: str, parent_id: str, *, folder: bool) -> Optional[Dict]:
        mime_clause = (
            f"mimeType = '{FOLDER_MIME}'" if folder else f"mimeType != '{FOLDER_MIME}'"
        )
        query = (
            f"name = '{_escape(name)}' and '{_escape(parent_id)}' in parents "
            f"and {mime_clause} and trashed = false"
        )
        response = self._call(
            self.service.files().list(
                q=query,
                spaces="drive",
                fields="files(id, name, webViewLink, size)",
                pageSize=10,
                **self._list_args(),
            ),
            what=f"buscar {'pasta' if folder else 'arquivo'} '{name}'",
        )
        files = response.get("files") or []
        return files[0] if files else None

    def ensure_folder(self, name: str, parent_id: str) -> str:
        """Retorna o id da subpasta `name` sob `parent_id`, criando se preciso."""
        cache_key = f"{parent_id}/{name}"
        if cache_key in self._folder_cache:
            return self._folder_cache[cache_key]

        existing = self._find_child(name, parent_id, folder=True)
        if existing:
            folder_id = existing["id"]
            LOG.debug("Drive: pasta '%s' já existe (%s).", name, folder_id)
        else:
            created = self._call(
                self.service.files().create(
                    body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
                    fields="id, name, webViewLink",
                    **self._shared_drive_args,
                ),
                what=f"criar pasta '{name}'",
            )
            folder_id = created["id"]
            LOG.info("Drive: pasta '%s' criada (%s).", name, folder_id)

        self._folder_cache[cache_key] = folder_id
        return folder_id

    def ensure_root(self) -> str:
        """Garante `medical-assets/ectoscopia` e devolve o id da pasta folha."""
        if self._root_id:
            return self._root_id

        parent = (
            self.settings.drive_root_parent_id
            or self.settings.drive_shared_drive_id
            or "root"
        )
        for part in [p for p in self.settings.drive_root_path.split("/") if p]:
            parent = self.ensure_folder(part, parent)

        self._root_id = parent
        LOG.info("Drive: raiz '%s' pronta (%s).", self.settings.drive_root_path, parent)
        return parent

    def ensure_actor_folder(self, actor_id: str) -> str:
        return self.ensure_folder(actor_id, self.ensure_root())

    def ensure_structure(self, actor_ids: List[str]) -> Dict[str, str]:
        """Cria a árvore inteira de uma vez e devolve {ator: folder_id}."""
        self.ensure_root()
        return {actor_id: self.ensure_actor_folder(actor_id) for actor_id in actor_ids}

    # ------------------------------------------------------------------ #
    # Upload
    # ------------------------------------------------------------------ #
    def upload(
        self,
        local_path: Path,
        parent_id: str,
        *,
        name: Optional[str] = None,
        make_public: Optional[bool] = None,
    ) -> UploadedFile:
        """Envia (ou atualiza) um arquivo e devolve id + `webViewLink`."""
        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:  # pragma: no cover
            raise DriveError("Instale `google-api-python-client`.") from exc

        if not local_path.is_file():
            raise FileNotFoundError(f"Arquivo para upload não encontrado: {local_path}")

        name = name or local_path.name
        mime = MIME_BY_SUFFIX.get(local_path.suffix.lower()) or (
            mimetypes.guess_type(name)[0] or "application/octet-stream"
        )
        size = local_path.stat().st_size
        # `resumable` só compensa em arquivos grandes; aqui tudo é ≤ 600 KB.
        media = MediaFileUpload(str(local_path), mimetype=mime, resumable=size > 5 * 1024 * 1024)
        existing = self._find_child(name, parent_id, folder=False)
        fields = "id, name, webViewLink, webContentLink, size"

        if existing:
            result = self._call(
                self.service.files().update(
                    fileId=existing["id"],
                    media_body=media,
                    fields=fields,
                    **self._shared_drive_args,
                ),
                what=f"atualizar '{name}'",
            )
            created = False
        else:
            result = self._call(
                self.service.files().create(
                    body={"name": name, "parents": [parent_id]},
                    media_body=media,
                    fields=fields,
                    **self._shared_drive_args,
                ),
                what=f"enviar '{name}'",
            )
            created = True

        file_id = result["id"]
        should_share = self.settings.drive_make_public if make_public is None else make_public
        if should_share:
            self._share_anyone(file_id, name)
            result = self._call(
                self.service.files().get(fileId=file_id, fields=fields, **self._shared_drive_args),
                what=f"reler link de '{name}'",
            )

        link = result.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
        LOG.info(
            "Drive: %s '%s' (%s) → %s",
            "enviado" if created else "atualizado", name, human_size(size), link,
        )
        return UploadedFile(
            file_id=file_id,
            name=name,
            web_view_link=link,
            size_bytes=size,
            created=created,
        )

    def _share_anyone(self, file_id: str, name: str) -> None:
        """Concede leitura para "qualquer pessoa com o link" (idempotente)."""
        try:
            self._call(
                self.service.permissions().create(
                    fileId=file_id,
                    body={"role": "reader", "type": "anyone"},
                    **self._shared_drive_args,
                ),
                what=f"compartilhar '{name}'",
            )
        except Exception as exc:  # noqa: BLE001
            # Políticas de domínio podem proibir link público — não é fatal.
            LOG.warning("Drive: não foi possível tornar '%s' público: %s", name, exc)

    # ------------------------------------------------------------------ #
    # Diagnóstico
    # ------------------------------------------------------------------ #
    def whoami(self) -> str:
        info = self._call(
            self.service.about().get(fields="user(emailAddress, displayName)"),
            what="about.get",
        )
        user = info.get("user") or {}
        return user.get("emailAddress") or user.get("displayName") or "desconhecido"


class NullDriveManager:
    """Stub usado com ``--skip-drive``: mantém a interface, não faz rede."""

    def ensure_structure(self, actor_ids: List[str]) -> Dict[str, str]:
        return {actor_id: "" for actor_id in actor_ids}

    def ensure_actor_folder(self, actor_id: str) -> str:
        return ""

    def ensure_root(self) -> str:
        return ""

    def upload(self, local_path: Path, parent_id: str, **_: object) -> UploadedFile:
        return UploadedFile(
            file_id="",
            name=local_path.name,
            web_view_link="",
            size_bytes=local_path.stat().st_size if local_path.is_file() else 0,
            created=False,
        )

    def whoami(self) -> str:
        return "(drive desabilitado)"
