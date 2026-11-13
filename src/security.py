"""Authentication, authorization, agency isolation, and input safety.

Design rules:

* API keys are never stored or returned in clear text. The registry keeps a key
  id, a random salt, and a SHA-256 digest; comparisons use ``hmac.compare_digest``.
* Every private endpoint resolves to one permission. Roles are additive:
  ``admin`` (everything), ``operator`` (operational writes), ``analyst`` (read).
* Private operator data belongs to one workspace agency. A principal from a
  different agency is refused on private permissions; administrators are the
  documented exception because they administer the workspace itself.
* Rate limiting, CORS validation, audit logging, sensitive-value redaction,
  input validation, and safe upload/ZIP handling live here so they can be
  tested without a web server.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import io
import json
import re
import secrets
import threading
import time
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROLE_ADMIN = "admin"
ROLE_OPERATOR = "operator"
ROLE_ANALYST = "analyst"
ROLES = (ROLE_ADMIN, ROLE_OPERATOR, ROLE_ANALYST)

PERMISSIONS = (
    "data:read",
    "data:write",
    "model:train",
    "forecast:run",
    "monthly:read",
    "monthly:write",
    "gtfs:read",
    "gtfs:write",
    "monitoring:read",
    "notifications:read",
    "notifications:write",
    "reports:read",
    "settings:write",
    "security:admin",
)

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_ADMIN: frozenset(PERMISSIONS),
    ROLE_OPERATOR: frozenset(
        {
            "data:read",
            "data:write",
            "model:train",
            "forecast:run",
            "monthly:read",
            "monthly:write",
            "gtfs:read",
            "gtfs:write",
            "monitoring:read",
            "notifications:read",
            "notifications:write",
            "reports:read",
            "settings:write",
        }
    ),
    ROLE_ANALYST: frozenset(
        {
            "data:read",
            "monthly:read",
            "gtfs:read",
            "monitoring:read",
            "notifications:read",
            "reports:read",
        }
    ),
}

REGISTRY_VERSION = 1
KEY_ID_PATTERN = re.compile(r"^[a-f0-9]{12}$")
AGENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{1,63}$")
PRINCIPAL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._@'-]{1,79}$")
ROUTE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/#-]{0,127}$")

PASSENGER_IDENTIFIER_FIELDS = frozenset(
    {
        "passenger_id",
        "passenger_name",
        "first_name",
        "last_name",
        "email",
        "phone",
        "address",
        "date_of_birth",
        "card_number",
        "ticket_id",
        "boarding_id",
        "latitude",
        "longitude",
        "device_id",
    }
)

SECRET_KEY_PATTERN = re.compile(r"(password|passwd|secret|token|api[_-]?key|authorization|credential|cookie)", re.I)
REDACTED = "***redacted***"
AUDIT_TEXT_LIMIT = 240


class SecurityError(Exception):
    """Base class for security failures mapped onto HTTP status codes."""

    status_code = 400

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class RegistryError(SecurityError):
    status_code = 503


class AuthenticationError(SecurityError):
    status_code = 401


class PermissionDenied(SecurityError):
    status_code = 403


class RateLimitExceeded(SecurityError):
    status_code = 429

    def __init__(self, detail: str, retry_after: float):
        super().__init__(detail)
        self.retry_after = max(0.0, float(retry_after))


class InputValidationError(SecurityError):
    status_code = 422


@dataclass(frozen=True)
class Principal:
    """The authenticated caller: never carries a key or digest."""

    id: str
    name: str
    role: str
    agency: str
    active: bool = True
    legacy: bool = False
    permissions: frozenset[str] = field(default_factory=frozenset)

    def allows(self, permission: str) -> bool:
        return permission in (self.permissions or ROLE_PERMISSIONS.get(self.role, frozenset()))


def public_principal(principal: Principal) -> dict[str, Any]:
    role_permissions = ROLE_PERMISSIONS.get(principal.role, frozenset())
    return {
        "id": principal.id,
        "name": principal.name,
        "role": principal.role,
        "agency": principal.agency,
        "active": principal.active,
        "legacy": principal.legacy,
        "permissions": sorted(role_permissions),
    }


def local_principal(agency: str, name: str = "Local operator") -> Principal:
    """Administrator identity used when hosted authentication is disabled."""
    return Principal(
        id="local-owner",
        name=name,
        role=ROLE_ADMIN,
        agency=agency,
        active=True,
        legacy=True,
        permissions=ROLE_PERMISSIONS[ROLE_ADMIN],
    )


# ---------------------------------------------------------------------------
# API keys and registry
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def hash_secret(secret: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{secret}".encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str, str]:
    """Return ``(full_key, key_id, salt, digest)``; the full key is shown once."""
    key_id = secrets.token_hex(6)
    salt = secrets.token_hex(16)
    secret = secrets.token_urlsafe(32)
    return f"bf_{key_id}_{secret}", key_id, salt, hash_secret(secret, salt)


def parse_api_key(presented: str) -> tuple[str, str] | None:
    if not presented or not presented.startswith("bf_"):
        return None
    remainder = presented[3:]
    if "_" not in remainder:
        return None
    key_id, secret = remainder.split("_", 1)
    if not KEY_ID_PATTERN.match(key_id) or len(secret) < 16:
        return None
    return key_id, secret


def default_registry(agency_code: str = "default", agency_name: str | None = None) -> dict[str, Any]:
    code = validate_agency_code(agency_code)
    return {
        "version": REGISTRY_VERSION,
        "workspace_agency": code,
        "agencies": [{"code": code, "name": agency_name or code, "created_at": _now_iso()}],
        "principals": [],
    }


def load_registry(path: str | Path, default_agency: str = "default") -> dict[str, Any]:
    """Load the registry, creating a bootstrap document if it does not exist.

    A corrupt registry fails closed: it is never silently replaced, because that
    would reset roles and keys without an administrator seeing it.
    """
    file_path = Path(path)
    if not file_path.exists():
        registry = default_registry(default_agency)
        save_registry(file_path, registry)
        return registry
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError("The security registry could not be read") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("principals"), list) or not payload.get("workspace_agency"):
        raise RegistryError("The security registry is not valid")
    payload.setdefault("agencies", [])
    payload.setdefault("version", REGISTRY_VERSION)
    return payload


def save_registry(path: str | Path, registry: dict[str, Any]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = file_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(file_path)


def principal_from_record(record: dict[str, Any]) -> Principal:
    role = str(record.get("role") or ROLE_ANALYST)
    if role not in ROLES:
        role = ROLE_ANALYST
    return Principal(
        id=str(record.get("id") or ""),
        name=str(record.get("name") or ""),
        role=role,
        agency=str(record.get("agency") or ""),
        active=bool(record.get("active", True)),
        legacy=False,
        permissions=ROLE_PERMISSIONS[role],
    )


def find_principal_by_key(registry: dict[str, Any], presented_key: str | None) -> Principal | None:
    """Return the matching principal, or None. Uses a constant-time digest compare."""
    if not presented_key:
        return None
    parsed = parse_api_key(presented_key)
    if parsed is None:
        return None
    key_id, secret = parsed
    for record in registry.get("principals", []):
        if not isinstance(record, dict) or record.get("key_id") != key_id:
            continue
        expected = str(record.get("key_hash") or "")
        actual = hash_secret(secret, str(record.get("salt") or ""))
        if hmac.compare_digest(expected, actual):
            return principal_from_record(record)
        return None
    return None


def key_matches_legacy(presented_key: str | None, expected: str | None) -> bool:
    """Constant-time comparison for the single legacy shared key."""
    if not presented_key or not expected:
        return False
    return hmac.compare_digest(presented_key.encode("utf-8"), expected.encode("utf-8"))


def validate_agency_code(value: str) -> str:
    text = str(value or "").strip()
    if not AGENCY_PATTERN.match(text):
        raise InputValidationError(
            "Agency codes must start with a letter or number and use 2-64 letters, numbers, spaces, dots, underscores, or hyphens"
        )
    return text


def find_agency(registry: dict[str, Any], code: str) -> dict[str, Any] | None:
    for agency in registry.get("agencies", []):
        if isinstance(agency, dict) and agency.get("code") == code:
            return agency
    return None


def create_agency(registry: dict[str, Any], code: str, name: str) -> dict[str, Any]:
    code = validate_agency_code(code)
    if find_agency(registry, code):
        raise InputValidationError(f"Agency {code} already exists")
    agency = {"code": code, "name": str(name or code).strip()[:120], "created_at": _now_iso()}
    registry.setdefault("agencies", []).append(agency)
    return agency


def public_agencies(registry: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "code": agency.get("code"),
            "name": agency.get("name"),
            "created_at": agency.get("created_at"),
            "principal_count": sum(1 for record in registry.get("principals", []) if record.get("agency") == agency.get("code")),
        }
        for agency in registry.get("agencies", [])
        if isinstance(agency, dict)
    ]


def public_principals(registry: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": record.get("id"),
            "name": record.get("name"),
            "role": record.get("role"),
            "agency": record.get("agency"),
            "active": bool(record.get("active", True)),
            "created_at": record.get("created_at"),
            "last_used_at": record.get("last_used_at"),
            "rotated_at": record.get("rotated_at"),
            "key_id": record.get("key_id"),
        }
        for record in registry.get("principals", [])
        if isinstance(record, dict)
    ]


def create_principal(registry: dict[str, Any], name: str, role: str, agency: str) -> tuple[dict[str, Any], str]:
    if role not in ROLES:
        raise InputValidationError(f"Role must be one of: {', '.join(ROLES)}")
    display = str(name or "").strip()
    if not PRINCIPAL_NAME_PATTERN.match(display):
        raise InputValidationError("Principal names must be 2-80 characters using letters, numbers, spaces, dots, underscores, @, apostrophes, or hyphens")
    agency_code = validate_agency_code(agency)
    if not find_agency(registry, agency_code):
        raise InputValidationError(f"Unknown agency: {agency_code}")
    if any(record.get("name") == display for record in registry.get("principals", []) if isinstance(record, dict)):
        raise InputValidationError(f"A principal named {display} already exists")

    full_key, key_id, salt, digest = generate_api_key()
    record = {
        "id": f"prn_{secrets.token_hex(8)}",
        "name": display,
        "role": role,
        "agency": agency_code,
        "active": True,
        "key_id": key_id,
        "salt": salt,
        "key_hash": digest,
        "created_at": _now_iso(),
        "last_used_at": None,
        "rotated_at": None,
    }
    registry.setdefault("principals", []).append(record)
    return record, full_key


def rotate_principal_key(registry: dict[str, Any], principal_id: str) -> tuple[dict[str, Any], str]:
    record = find_principal_record(registry, principal_id)
    full_key, key_id, salt, digest = generate_api_key()
    record.update({"key_id": key_id, "salt": salt, "key_hash": digest, "rotated_at": _now_iso()})
    return record, full_key


def deactivate_principal(registry: dict[str, Any], principal_id: str) -> dict[str, Any]:
    record = find_principal_record(registry, principal_id)
    record["active"] = False
    record["deactivated_at"] = _now_iso()
    return record


def find_principal_record(registry: dict[str, Any], principal_id: str) -> dict[str, Any]:
    for record in registry.get("principals", []):
        if isinstance(record, dict) and record.get("id") == principal_id:
            return record
    raise InputValidationError("Principal not found")


def touch_principal(registry: dict[str, Any], principal_id: str) -> None:
    for record in registry.get("principals", []):
        if isinstance(record, dict) and record.get("id") == principal_id:
            record["last_used_at"] = _now_iso()
            return


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def require(principal: Principal, permission: str, *, workspace_agency: str | None = None) -> None:
    """Raise unless ``principal`` may use ``permission`` in this workspace."""
    if not principal.active:
        raise AuthenticationError("This API key is inactive")
    if permission not in PERMISSIONS:
        raise PermissionDenied("Unknown permission")
    if not principal.allows(permission):
        raise PermissionDenied(f"Role {principal.role} cannot use {permission}")
    if workspace_agency and principal.role != ROLE_ADMIN and principal.agency != workspace_agency:
        raise PermissionDenied("This workspace belongs to another agency")


def assert_agency_access(principal: Principal, agency: str, *, workspace_agency: str) -> str:
    """Scope an agency-scoped resource (for example an imported GTFS feed)."""
    requested = validate_agency_code(agency)
    if principal.role == ROLE_ADMIN:
        return requested
    if requested != principal.agency:
        raise PermissionDenied("Agency-scoped data belongs to another agency")
    if principal.agency != workspace_agency:
        raise PermissionDenied("This workspace belongs to another agency")
    return requested


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class RateLimiter:
    """Sliding-window limiter. Returns 0.0 when allowed, else seconds to wait."""

    def __init__(self, limit: int = 240, window_seconds: float = 60.0, max_keys: int = 10_000):
        if limit < 1 or window_seconds <= 0:
            raise ValueError("limit and window_seconds must be positive")
        self.limit = int(limit)
        self.window_seconds = float(window_seconds)
        self.max_keys = int(max_keys)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> float:
        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            if len(self._hits) > self.max_keys:
                self._hits.clear()
            hits = self._hits.setdefault(key, deque())
            cutoff = timestamp - self.window_seconds
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                retry_after = self.window_seconds - (timestamp - hits[0])
                return max(retry_after, 0.001)
            hits.append(timestamp)
            return 0.0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


def cors_settings(raw: str | None) -> dict[str, Any]:
    """Parse an explicit origin allowlist. Wildcards are refused."""
    origins = [item.strip() for item in (raw or "").split(",") if item.strip()]
    cleaned: list[str] = []
    for origin in origins:
        if origin == "*":
            raise InputValidationError("Wildcard CORS origins are not allowed")
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise InputValidationError(f"CORS origin must be an absolute http(s) origin: {origin}")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise InputValidationError(f"CORS origin must not include a path, query, or fragment: {origin}")
        normalized = f"{parsed.scheme}://{parsed.netloc}".lower()
        if normalized not in cleaned:
            cleaned.append(normalized)
    return {
        "allow_origins": cleaned,
        "allow_credentials": False,
        "allow_methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        "allow_headers": ["Authorization", "Content-Type", "Accept"],
        "max_age": 600,
    }


# ---------------------------------------------------------------------------
# Redaction and audit
# ---------------------------------------------------------------------------


def redact(value: Any) -> Any:
    """Recursively replace secret-looking values and truncate long strings."""
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if SECRET_KEY_PATTERN.search(str(key)):
                result[key] = REDACTED
            else:
                result[key] = redact(item)
        return result
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return value if len(value) <= AUDIT_TEXT_LIMIT else value[:AUDIT_TEXT_LIMIT] + "…"
    return value


def audit_event(
    *,
    action: str,
    outcome: str,
    principal: Principal | None = None,
    target: str | None = None,
    detail: Any = None,
    method: str | None = None,
    path: str | None = None,
    client: str | None = None,
    status_code: int | None = None,
) -> dict[str, Any]:
    return {
        "timestamp_utc": _now_iso(),
        "action": str(action)[:120],
        "outcome": outcome,
        "principal_id": principal.id if principal else None,
        "principal_name": principal.name if principal else None,
        "role": principal.role if principal else None,
        "agency": principal.agency if principal else None,
        "target": str(target)[:200] if target else None,
        "method": method,
        "path": path,
        "client": client,
        "status_code": status_code,
        "detail": redact(detail) if detail is not None else None,
    }


def append_audit(path: str | Path, event: dict[str, Any], max_bytes: int = 5 * 1024 * 1024) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with file_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")
    try:
        if file_path.stat().st_size > max_bytes:
            _trim_audit(file_path, max_bytes // 2)
    except OSError:
        return


def _trim_audit(path: Path, keep_bytes: int) -> None:
    data = path.read_bytes()
    tail = data[-keep_bytes:]
    newline = tail.find(b"\n")
    trimmed = tail[newline + 1 :] if newline >= 0 else tail
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(trimmed)
    temporary.replace(path)


def read_audit(path: str | Path, limit: int = 100) -> list[dict[str, Any]]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()[-max(int(limit), 1) :]
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    events.reverse()
    return events


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def validate_route_id(value: str) -> str:
    text = str(value or "").strip()
    if not ROUTE_ID_PATTERN.match(text):
        raise InputValidationError("Route identifiers must be 1-128 characters and cannot contain control characters")
    return text


def validate_iso_date(value: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = dt.date.fromisoformat(text)
    except ValueError as exc:
        raise InputValidationError("Dates must use YYYY-MM-DD") from exc
    return parsed.isoformat()


def validate_date_range(start: str, end: str) -> tuple[str, str]:
    first = validate_iso_date(start)
    last = validate_iso_date(end)
    if first > last:
        raise InputValidationError("The start date must not be after the end date")
    return first, last


def ensure_no_passenger_identifiers(payload: Any) -> None:
    """Refuse payloads that look like they carry individual passenger records."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).strip().lower() in PASSENGER_IDENTIFIER_FIELDS:
                    found.add(str(key).strip().lower())
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)

    walk(payload)
    if found:
        raise InputValidationError(
            "Individual passenger identifiers are not accepted: " + ", ".join(sorted(found))
        )


# ---------------------------------------------------------------------------
# Uploads and ZIP extraction
# ---------------------------------------------------------------------------

ALLOWED_UPLOAD_EXTENSIONS = frozenset({".csv", ".json", ".zip", ".txt"})
DEFAULT_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_ZIP_ENTRIES = 300
DEFAULT_MAX_UNCOMPRESSED_BYTES = 300 * 1024 * 1024
DEFAULT_MAX_COMPRESSION_RATIO = 200
RATIO_CHECK_MIN_BYTES = 1024 * 1024


def validate_upload_name(filename: str | None, *, allowed_extensions: frozenset[str] = ALLOWED_UPLOAD_EXTENSIONS) -> str:
    """Return a safe basename or raise. Rejects traversal, hidden files, and odd extensions."""
    if not filename:
        raise InputValidationError("A file name is required")
    if "\x00" in filename:
        raise InputValidationError("The file name contains invalid characters")
    text = filename.replace("\\", "/")
    name = Path(text).name
    if name in {"", ".", ".."} or name != text or text.startswith("/"):
        raise InputValidationError("The file name must not contain a path")
    if name.startswith("."):
        raise InputValidationError("Hidden files are not accepted")
    suffix = Path(name).suffix.lower()
    if suffix not in allowed_extensions:
        raise InputValidationError(f"Unsupported file type: {suffix or 'none'}")
    return name


def validate_upload_payload(
    contents: bytes,
    *,
    filename: str | None = None,
    max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> str:
    if not contents:
        raise InputValidationError("The uploaded file is empty")
    if len(contents) > max_bytes:
        raise InputValidationError(f"Upload exceeds the {max_bytes // (1024 * 1024)} MB limit")
    return validate_upload_name(filename) if filename is not None else "upload"


def _zip_member_is_symlink(info: zipfile.ZipInfo) -> bool:
    # Unix mode lives in the top 16 bits of external_attr.
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def inspect_zip_payload(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    max_entries: int = DEFAULT_MAX_ZIP_ENTRIES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_ratio: int = DEFAULT_MAX_COMPRESSION_RATIO,
) -> list[str]:
    """Validate a ZIP before extraction; returns member names or raises."""
    if not data:
        raise InputValidationError("The ZIP upload is empty")
    if len(data) > max_bytes:
        raise InputValidationError(f"ZIP upload exceeds the {max_bytes // (1024 * 1024)} MB limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise InputValidationError("The uploaded file is not a valid ZIP archive") from exc

    with archive:
        members = archive.infolist()
        if len(members) > max_entries:
            raise InputValidationError(f"ZIP contains more than {max_entries} entries")
        total_uncompressed = 0
        names: list[str] = []
        for info in members:
            name = info.filename
            if info.flag_bits & 0x1:
                raise InputValidationError(f"Encrypted ZIP entries are not supported: {name}")
            normalized = name.replace("\\", "/")
            if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
                raise InputValidationError(f"Absolute paths are not allowed in ZIP entries: {name}")
            segments = [segment for segment in normalized.split("/") if segment not in {"", "."}]
            if any(segment == ".." for segment in segments):
                raise InputValidationError(f"Parent-directory paths are not allowed in ZIP entries: {name}")
            if _zip_member_is_symlink(info):
                raise InputValidationError(f"Symlinks are not allowed in ZIP entries: {name}")
            total_uncompressed += max(int(info.file_size), 0)
            if total_uncompressed > max_uncompressed_bytes:
                raise InputValidationError("ZIP expands beyond the allowed uncompressed size")
            if info.file_size >= RATIO_CHECK_MIN_BYTES and info.compress_size > 0:
                if info.file_size / info.compress_size > max_ratio:
                    raise InputValidationError(f"ZIP entry has a suspicious compression ratio: {name}")
            if not info.is_dir():
                names.append(normalized)
        return names


def safe_extract_zip(
    data: bytes,
    destination: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    max_entries: int = DEFAULT_MAX_ZIP_ENTRIES,
    max_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
    max_ratio: int = DEFAULT_MAX_COMPRESSION_RATIO,
) -> list[Path]:
    """Extract a validated ZIP, refusing anything that escapes ``destination``."""
    names = inspect_zip_payload(
        data,
        max_bytes=max_bytes,
        max_entries=max_entries,
        max_uncompressed_bytes=max_uncompressed_bytes,
        max_ratio=max_ratio,
    )
    target_root = Path(destination).resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in names:
            target = (target_root / name).resolve()
            if target_root not in target.parents and target != target_root:
                raise InputValidationError(f"ZIP entry escapes the destination: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, target.open("wb") as sink:
                remaining = max_uncompressed_bytes
                while True:
                    chunk = source.read(1024 * 256)
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise InputValidationError("ZIP expands beyond the allowed uncompressed size")
                    sink.write(chunk)
            written.append(target)
    return written
