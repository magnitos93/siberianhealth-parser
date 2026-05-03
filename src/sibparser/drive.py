"""Google Drive client: OAuth flow, mirrored folder hierarchy, file upload with
content-hash dedup that turns repeated certificates into Drive shortcuts.

The OAuth flow uses :mod:`google_auth_oauthlib`. The first time you call
:meth:`DriveClient.authorize`, a browser tab opens for consent and the obtained
refresh token is cached to ``settings.google_token``.

Dedup strategy:
* Each file we download gets a SHA-256 hash.
* If a file with the same hash was uploaded before in another product folder we
  create a Drive **shortcut** in the new product's folder pointing at the
  existing file (so the same certificate is stored once on Drive).
"""
from __future__ import annotations

import hashlib
import io
import logging
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import Resource, build
from googleapiclient.http import MediaIoBaseUpload

from .state import State

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
SHARED_FILES_PATH_SUFFIX = "_shared"


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def load_credentials(client_secret_path: Path, token_path: Path) -> Credentials:
    """Return Drive credentials, running an OAuth flow if needed.

    Raises :class:`FileNotFoundError` if the client secret is missing.
    """
    if not client_secret_path.exists():
        raise FileNotFoundError(
            f"Google OAuth client secret not found at {client_secret_path}. "
            "Download it from https://console.cloud.google.com (OAuth client ID -> Desktop app)."
        )

    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
        return creds

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    token_path.write_text(creds.to_json())
    return creds


# ---------------------------------------------------------------------------
# Drive client
# ---------------------------------------------------------------------------


@dataclass
class UploadResult:
    drive_id: str
    parent_id: str
    is_shortcut: bool
    sha256: str | None


class DriveClient:
    def __init__(
        self,
        credentials: Credentials,
        state: State,
        root_folder_name: str = "SiberianHealthParser",
    ) -> None:
        self._service: Resource = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self.state = state
        self.root_folder_name = root_folder_name
        self._root_id: str | None = None

    # -- folders --------------------------------------------------------

    @property
    def root_id(self) -> str:
        if self._root_id is None:
            self._root_id = self._ensure_root_folder()
        return self._root_id

    def _ensure_root_folder(self) -> str:
        cached = self.state.get_folder("")
        if cached:
            return cached
        # Try to find it by name in My Drive root
        q = (
            f"name = {_q(self.root_folder_name)} and "
            "mimeType = 'application/vnd.google-apps.folder' and "
            "trashed = false and 'root' in parents"
        )
        resp = self._service.files().list(
            q=q,
            fields="files(id, name)",
            pageSize=1,
        ).execute()
        files = resp.get("files", [])
        if files:
            folder_id = files[0]["id"]
        else:
            folder_id = self._create_folder(self.root_folder_name, parent_id="root")
        self.state.remember_folder("", folder_id)
        return folder_id

    def _create_folder(self, name: str, parent_id: str) -> str:
        body = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        f = self._service.files().create(body=body, fields="id").execute()
        return f["id"]

    def ensure_path(self, parts: list[str]) -> str:
        """Create / get a folder by its slash-separated path under the root."""
        cleaned = [p.strip() for p in parts if p and p.strip()]
        full_path = "/".join(cleaned)
        cached = self.state.get_folder(full_path)
        if cached:
            return cached

        parent_id = self.root_id
        accumulated: list[str] = []
        for part in cleaned:
            accumulated.append(part)
            sub_path = "/".join(accumulated)
            sub_id = self.state.get_folder(sub_path)
            if sub_id:
                parent_id = sub_id
                continue
            # Look for existing folder with this name under parent
            q = (
                f"name = {_q(part)} and "
                "mimeType = 'application/vnd.google-apps.folder' and "
                f"trashed = false and {_q(parent_id)} in parents"
            )
            resp = self._service.files().list(
                q=q,
                fields="files(id, name)",
                pageSize=1,
            ).execute()
            files = resp.get("files", [])
            if files:
                folder_id = files[0]["id"]
            else:
                folder_id = self._create_folder(part, parent_id=parent_id)
            self.state.remember_folder(sub_path, folder_id)
            parent_id = folder_id
        return parent_id

    # -- files ----------------------------------------------------------

    def upload_text(self, name: str, text: str, parent_id: str) -> str:
        media = MediaIoBaseUpload(io.BytesIO(text.encode("utf-8")), mimetype="text/plain", resumable=False)
        file = self._service.files().create(
            body={"name": name, "parents": [parent_id]},
            media_body=media,
            fields="id",
        ).execute()
        return file["id"]

    def upload_or_link(
        self,
        source_url: str,
        target_name: str,
        target_parent_id: str,
        shared_parent_id: str | None = None,
        timeout: float = 60.0,
    ) -> UploadResult:
        """Download a remote file and upload it to Drive (with dedup).

        Dedup logic (in order):
          1. If we have already seen this exact ``source_url``, create a
             shortcut in the new parent and return.
          2. Otherwise fetch the bytes, compute SHA-256.
          3. If a file with the same SHA-256 was uploaded before, create a
             shortcut.
          4. Otherwise upload to ``shared_parent_id`` (if given) or directly
             into ``target_parent_id``, and remember the URL+hash.
        """
        existing = self.state.lookup_file_by_url(source_url)
        if existing:
            shortcut_id = self._create_shortcut(
                target_name, target_parent_id, existing["drive_file_id"]
            )
            return UploadResult(
                drive_id=shortcut_id,
                parent_id=target_parent_id,
                is_shortcut=True,
                sha256=existing.get("sha256"),
            )

        try:
            content = self._download(source_url, timeout=timeout)
        except Exception as exc:
            raise UploadError(f"Failed to download {source_url}: {exc}") from exc

        sha = hashlib.sha256(content).hexdigest()
        existing_hash = self.state.lookup_file_by_sha256(sha)
        if existing_hash:
            shortcut_id = self._create_shortcut(
                target_name, target_parent_id, existing_hash["drive_file_id"]
            )
            self.state.remember_file(
                source_url=source_url,
                sha256=sha,
                drive_file_id=existing_hash["drive_file_id"],
                drive_parent_id=existing_hash["drive_parent_id"],
                name=target_name,
                size_bytes=len(content),
            )
            return UploadResult(
                drive_id=shortcut_id,
                parent_id=target_parent_id,
                is_shortcut=True,
                sha256=sha,
            )

        parent = shared_parent_id or target_parent_id
        mime, _ = mimetypes.guess_type(target_name)
        media = MediaIoBaseUpload(
            io.BytesIO(content),
            mimetype=mime or "application/octet-stream",
            resumable=False,
        )
        file = self._service.files().create(
            body={"name": target_name, "parents": [parent]},
            media_body=media,
            fields="id",
        ).execute()
        drive_file_id = file["id"]

        self.state.remember_file(
            source_url=source_url,
            sha256=sha,
            drive_file_id=drive_file_id,
            drive_parent_id=parent,
            name=target_name,
            size_bytes=len(content),
        )

        if shared_parent_id and shared_parent_id != target_parent_id:
            shortcut_id = self._create_shortcut(target_name, target_parent_id, drive_file_id)
            return UploadResult(
                drive_id=shortcut_id,
                parent_id=target_parent_id,
                is_shortcut=True,
                sha256=sha,
            )

        return UploadResult(
            drive_id=drive_file_id, parent_id=parent, is_shortcut=False, sha256=sha
        )

    def _download(self, url: str, timeout: float) -> bytes:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.get(url, headers={"User-Agent": "sibparser/0.1"})
            r.raise_for_status()
            return r.content

    def _create_shortcut(self, name: str, parent_id: str, target_file_id: str) -> str:
        body = {
            "name": name,
            "mimeType": "application/vnd.google-apps.shortcut",
            "parents": [parent_id],
            "shortcutDetails": {"targetId": target_file_id},
        }
        f = self._service.files().create(body=body, fields="id").execute()
        return f["id"]


class UploadError(RuntimeError):
    pass


def _q(s: str) -> str:
    """Quote a string for a Drive ``q=`` filter."""
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


# Convenience wrapper used by runner / web app.
def open_drive(client_secret_path: Path, token_path: Path, state: State, root_folder_name: str) -> DriveClient:
    creds = load_credentials(client_secret_path, token_path)
    return DriveClient(creds, state=state, root_folder_name=root_folder_name)


__all__ = [
    "SHARED_FILES_PATH_SUFFIX",
    "DriveClient",
    "UploadError",
    "UploadResult",
    "load_credentials",
    "open_drive",
    "os",  # exposed for tests that may patch environment
]
