"""Thin wrapper around the Google Drive v3 client.

Two responsibilities:
- download a file by drive_id into a local path, returning the chosen local extension
- upload a local file into a target Drive folder, returning (drive_id, webViewLink)

The SA JSON is never logged. Errors are mapped to user-readable messages so
that callbacks reaching Make can be understood by the audiovisual team.
"""
from __future__ import annotations

import base64
import io
import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from .settings import get_settings

logger = logging.getLogger(__name__)

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

# Conservative mime → extension fallback for common video/audio types.
# We prefer the file's actual name extension when present.
_MIME_EXT = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/aac": ".aac",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
}


class DriveError(RuntimeError):
    """Surfaced to the user; the message should be human-readable."""


@dataclass(frozen=True)
class DownloadResult:
    local_path: str
    drive_name: str
    mime_type: str


@dataclass(frozen=True)
class UploadResult:
    drive_id: str
    web_view_link: str


def _load_sa_info() -> dict[str, Any]:
    settings = get_settings()
    # Path takes precedence — it's what Easypanel/Docker secret volumes look like.
    if settings.google_service_account_json:
        with open(settings.google_service_account_json, "r", encoding="utf-8") as fh:
            return json.load(fh)
    if settings.google_service_account_json_b64:
        raw = base64.b64decode(settings.google_service_account_json_b64)
        return json.loads(raw)
    raise DriveError(
        "Service Account no configurada — define GOOGLE_SERVICE_ACCOUNT_JSON o "
        "GOOGLE_SERVICE_ACCOUNT_JSON_B64."
    )


@lru_cache(maxsize=1)
def _service() -> Any:
    info = _load_sa_info()
    # NEVER log `info` — it is a private key.
    creds = Credentials.from_service_account_info(info, scopes=DRIVE_SCOPES)
    # cache_discovery=False avoids a noisy warning in containers.
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _resolve_extension(name: str, mime_type: str, fallback: str) -> str:
    if "." in name:
        ext = "." + name.rsplit(".", 1)[1].lower()
        if 1 < len(ext) <= 6:
            return ext
    return _MIME_EXT.get(mime_type, fallback)


def download_file(drive_id: str, dest_dir: str, base_name: str, fallback_ext: str) -> DownloadResult:
    """Download a Drive file into dest_dir/base_name<ext>.

    The extension is picked from the file name on Drive when present, otherwise
    from the mime type, otherwise the fallback. Raises DriveError on failure.
    """
    svc = _service()
    try:
        meta = (
            svc.files()
            .get(fileId=drive_id, fields="name,mimeType,size", supportsAllDrives=True)
            .execute()
        )
    except HttpError as exc:
        raise DriveError(_explain_http_error(exc, drive_id, action="leer metadata")) from exc
    except Exception as exc:
        raise DriveError(f"No pude leer metadata del archivo {drive_id} en Drive.") from exc

    name = meta.get("name", "")
    mime = meta.get("mimeType", "")
    ext = _resolve_extension(name, mime, fallback_ext)
    local_path = f"{dest_dir}/{base_name}{ext}"

    try:
        request = svc.files().get_media(fileId=drive_id, supportsAllDrives=True)
        with io.FileIO(local_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk()
    except HttpError as exc:
        raise DriveError(_explain_http_error(exc, drive_id, action="descargar")) from exc
    except Exception as exc:
        raise DriveError(f"Fallo descargando el archivo {drive_id} desde Drive.") from exc

    return DownloadResult(local_path=local_path, drive_name=name, mime_type=mime)


def upload_file(
    local_path: str,
    folder_drive_id: str,
    target_name: str,
    mime_type: str = "video/mp4",
) -> UploadResult:
    svc = _service()
    metadata = {"name": target_name, "parents": [folder_drive_id]}
    try:
        media = MediaFileUpload(local_path, mimetype=mime_type, resumable=True, chunksize=8 * 1024 * 1024)
        created = (
            svc.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
    except HttpError as exc:
        raise DriveError(_explain_http_error(exc, folder_drive_id, action="subir output")) from exc
    except Exception as exc:
        raise DriveError(f"Fallo subiendo el output a la carpeta {folder_drive_id} en Drive.") from exc

    drive_id = created.get("id")
    link = created.get("webViewLink") or f"https://drive.google.com/file/d/{drive_id}/view"
    if not drive_id:
        raise DriveError("Drive devolvió una respuesta inesperada (sin id) al subir el output.")
    return UploadResult(drive_id=drive_id, web_view_link=link)


def _explain_http_error(exc: HttpError, drive_id: str, action: str) -> str:
    status = getattr(exc.resp, "status", None)
    if status == 404:
        return (
            f"No pude {action} el archivo {drive_id}: no existe o la Service "
            f"Account no tiene acceso. Comparte el archivo o su carpeta con el "
            f"email de la Service Account."
        )
    if status == 403:
        return (
            f"No pude {action} el archivo {drive_id}: permiso denegado por Drive. "
            f"Comparte el archivo o su carpeta con la Service Account con permisos "
            f"de Editor."
        )
    if status == 401:
        return (
            f"No pude {action} el archivo {drive_id}: credenciales de la Service "
            f"Account inválidas o sin scopes de Drive."
        )
    return f"Fallo de Drive ({status}) al {action} el archivo {drive_id}."
