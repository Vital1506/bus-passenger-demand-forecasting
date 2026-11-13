"""Deterministic tests for the Phase 9 security layer."""

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from src import security


def build_zip(entries: dict[str, bytes], *, symlinks=(), compress: bool = True) -> bytes:
    buffer = io.BytesIO()
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buffer, "w", mode) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
        for name in symlinks:
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, "/etc/passwd")
    return buffer.getvalue()


class KeyTests(unittest.TestCase):
    def test_generate_and_parse(self):
        full_key, key_id, salt, digest = security.generate_api_key()
        self.assertTrue(full_key.startswith("bf_"))
        self.assertTrue(security.KEY_ID_PATTERN.match(key_id))
        self.assertEqual(len(salt), 32)
        self.assertEqual(len(digest), 64)
        parsed = security.parse_api_key(full_key)
        self.assertEqual(parsed[0], key_id)

    def test_parse_rejects_malformed_keys(self):
        for value in ("", "not-a-key", "bf_short_secret", "bf_ZZZZZZZZZZZZ_secretsecretsecret", "bf_0123456789ab_"):
            self.assertIsNone(security.parse_api_key(value), value)

    def test_hash_is_salt_dependent_and_deterministic(self):
        first = security.hash_secret("secret", "salt-a")
        self.assertEqual(first, security.hash_secret("secret", "salt-a"))
        self.assertNotEqual(first, security.hash_secret("secret", "salt-b"))
        self.assertNotEqual(first, security.hash_secret("other", "salt-a"))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "security.json"
        self.registry = security.load_registry(self.path, default_agency="AGENCY-A")
        self.full_key, _ = self._add_principal("Ops", "operator", "AGENCY-A")

    def tearDown(self):
        self.tmp.cleanup()

    def _add_principal(self, name, role, agency):
        record, key = security.create_principal(self.registry, name, role, agency)
        security.save_registry(self.path, self.registry)
        return key, record

    def test_bootstrap_creates_workspace_agency(self):
        self.assertEqual(self.registry["workspace_agency"], "AGENCY-A")
        self.assertEqual(security.public_agencies(self.registry)[0]["code"], "AGENCY-A")
        self.assertTrue(self.path.exists())

    def test_corrupt_registry_fails_closed(self):
        self.path.write_text("{broken")
        with self.assertRaises(security.RegistryError):
            security.load_registry(self.path)

    def test_principal_record_stores_no_plaintext(self):
        stored = self.path.read_text(encoding="utf-8")
        self.assertNotIn(self.full_key, stored)
        secret = security.parse_api_key(self.full_key)[1]
        self.assertNotIn(secret, stored)
        entry = self.registry["principals"][0]
        self.assertEqual(entry["key_hash"], security.hash_secret(secret, entry["salt"]))

    def test_public_principals_hide_digests(self):
        public = json.dumps(security.public_principals(self.registry))
        self.assertNotIn("key_hash", public)
        self.assertNotIn("salt\"", public)

    def test_authentication_by_key(self):
        principal = security.find_principal_by_key(self.registry, self.full_key)
        self.assertIsNotNone(principal)
        self.assertEqual(principal.name, "Ops")
        self.assertEqual(principal.role, "operator")
        self.assertEqual(principal.agency, "AGENCY-A")
        self.assertIsNone(security.find_principal_by_key(self.registry, self.full_key[:-2] + "xy"))
        self.assertIsNone(security.find_principal_by_key(self.registry, "bf_ffffffffffff_unknown"))

    def test_rotation_invalidates_old_key(self):
        principal_id = self.registry["principals"][0]["id"]
        _, new_key = security.rotate_principal_key(self.registry, principal_id)
        security.save_registry(self.path, self.registry)
        self.assertIsNone(security.find_principal_by_key(self.registry, self.full_key))
        self.assertIsNotNone(security.find_principal_by_key(self.registry, new_key))

    def test_deactivation_blocks_authentication_and_authorization(self):
        principal_id = self.registry["principals"][0]["id"]
        security.deactivate_principal(self.registry, principal_id)
        with self.assertRaises(security.AuthenticationError):
            security.require(
                security.find_principal_by_key(self.registry, self.full_key), "data:read"
            )

    def test_unknown_principal_and_agency_are_rejected(self):
        with self.assertRaises(security.InputValidationError):
            security.create_principal(self.registry, "Ghost", "operator", "AGENCY-Z")
        with self.assertRaises(security.InputValidationError):
            security.create_principal(self.registry, "Ops", "operator", "AGENCY-A")
        with self.assertRaises(security.InputValidationError):
            security.create_principal(self.registry, "Bad Role", "superuser", "AGENCY-A")
        with self.assertRaises(security.InputValidationError):
            security.rotate_principal_key(self.registry, "missing")

    def test_agency_duplicate_rejected(self):
        with self.assertRaises(security.InputValidationError):
            security.create_agency(self.registry, "AGENCY-A", "Duplicate")
        created = security.create_agency(self.registry, "AGENCY-B", "Second operator")
        self.assertEqual(created["code"], "AGENCY-B")
        self.assertEqual(len(security.public_agencies(self.registry)), 2)

    def test_touch_principal_records_last_use(self):
        security.touch_principal(self.registry, self.registry["principals"][0]["id"])
        self.assertIsNotNone(self.registry["principals"][0]["last_used_at"])


class AuthorizationTests(unittest.TestCase):
    def test_admin_has_every_permission(self):
        principal = security.Principal("a", "Admin", "admin", "A", permissions=security.ROLE_PERMISSIONS["admin"])
        for permission in security.PERMISSIONS:
            security.require(principal, permission, workspace_agency="A")

    def test_operator_can_write_data_but_not_administer_security(self):
        principal = security.Principal("o", "Ops", "operator", "A", permissions=security.ROLE_PERMISSIONS["operator"])
        security.require(principal, "data:write", workspace_agency="A")
        security.require(principal, "model:train", workspace_agency="A")
        with self.assertRaises(security.PermissionDenied):
            security.require(principal, "security:admin", workspace_agency="A")

    def test_analyst_is_read_only(self):
        principal = security.Principal("r", "Analyst", "analyst", "A", permissions=security.ROLE_PERMISSIONS["analyst"])
        for permission in ("data:read", "reports:read", "monitoring:read", "notifications:read"):
            security.require(principal, permission, workspace_agency="A")
        for permission in ("data:write", "model:train", "forecast:run", "notifications:write", "settings:write"):
            with self.assertRaises(security.PermissionDenied):
                security.require(principal, permission, workspace_agency="A")

    def test_other_agency_is_denied_private_data(self):
        principal = security.Principal("x", "Other", "operator", "AGENCY-B", permissions=security.ROLE_PERMISSIONS["operator"])
        with self.assertRaises(security.PermissionDenied) as context:
            security.require(principal, "data:read", workspace_agency="AGENCY-A")
        self.assertIn("another agency", context.exception.detail)

    def test_administrator_is_documented_exception(self):
        principal = security.Principal("a", "Admin", "admin", "AGENCY-B", permissions=security.ROLE_PERMISSIONS["admin"])
        security.require(principal, "data:write", workspace_agency="AGENCY-A")

    def test_agency_scoped_resources(self):
        operator = security.Principal("o", "Ops", "operator", "AGENCY-A", permissions=security.ROLE_PERMISSIONS["operator"])
        self.assertEqual(security.assert_agency_access(operator, "AGENCY-A", workspace_agency="AGENCY-A"), "AGENCY-A")
        with self.assertRaises(security.PermissionDenied):
            security.assert_agency_access(operator, "AGENCY-B", workspace_agency="AGENCY-A")
        admin = security.Principal("a", "Admin", "admin", "AGENCY-A", permissions=security.ROLE_PERMISSIONS["admin"])
        self.assertEqual(security.assert_agency_access(admin, "AGENCY-B", workspace_agency="AGENCY-A"), "AGENCY-B")

    def test_local_principal_is_administrator(self):
        principal = security.local_principal("AGENCY-A")
        self.assertEqual(principal.role, "admin")
        security.require(principal, "security:admin", workspace_agency="AGENCY-A")


class RateLimiterTests(unittest.TestCase):
    def test_blocks_after_limit_and_reports_wait(self):
        limiter = security.RateLimiter(limit=3, window_seconds=60)
        for _ in range(3):
            self.assertEqual(limiter.allow("k", now=100.0), 0.0)
        wait = limiter.allow("k", now=100.0)
        self.assertGreater(wait, 0.0)
        self.assertLessEqual(wait, 60.0)

    def test_window_expiry_and_key_isolation(self):
        limiter = security.RateLimiter(limit=1, window_seconds=10)
        self.assertEqual(limiter.allow("a", now=0.0), 0.0)
        self.assertGreater(limiter.allow("a", now=1.0), 0.0)
        self.assertEqual(limiter.allow("b", now=1.0), 0.0)
        self.assertEqual(limiter.allow("a", now=11.0), 0.0)

    def test_reset(self):
        limiter = security.RateLimiter(limit=1, window_seconds=10)
        limiter.allow("a", now=0.0)
        limiter.reset("a")
        self.assertEqual(limiter.allow("a", now=0.0), 0.0)

    def test_rejects_invalid_configuration(self):
        with self.assertRaises(ValueError):
            security.RateLimiter(limit=0)
        with self.assertRaises(ValueError):
            security.RateLimiter(window_seconds=0)


class CorsTests(unittest.TestCase):
    def test_valid_origins_are_normalized(self):
        settings = security.cors_settings("https://App.Example.com, http://127.0.0.1:8765/, https://app.example.com")
        self.assertEqual(settings["allow_origins"], ["https://app.example.com", "http://127.0.0.1:8765"])
        self.assertFalse(settings["allow_credentials"])
        self.assertIn("Authorization", settings["allow_headers"])

    def test_wildcard_and_paths_are_rejected(self):
        for raw in ("*", "https://app.example.com/path", "ftp://app.example.com", "app.example.com", "https://app.example.com?x=1"):
            with self.assertRaises(security.InputValidationError, msg=raw):
                security.cors_settings(raw)

    def test_empty_configuration_allows_nothing(self):
        self.assertEqual(security.cors_settings("")["allow_origins"], [])
        self.assertEqual(security.cors_settings(None)["allow_origins"], [])


class RedactionAndAuditTests(unittest.TestCase):
    def test_redact_hides_secret_keys_recursively(self):
        payload = {
            "email": {"password": "p", "token": "t", "recipients": ["ops@example.com"]},
            "authorization": "Bearer abc",
            "nested": [{"api_key": "k", "safe": "value"}],
            "long": "x" * 500,
        }
        redacted = security.redact(payload)
        self.assertEqual(redacted["email"]["password"], security.REDACTED)
        self.assertEqual(redacted["email"]["token"], security.REDACTED)
        self.assertEqual(redacted["authorization"], security.REDACTED)
        self.assertEqual(redacted["nested"][0]["api_key"], security.REDACTED)
        self.assertEqual(redacted["nested"][0]["safe"], "value")
        self.assertLessEqual(len(redacted["long"]), security.AUDIT_TEXT_LIMIT + 1)

    def test_audit_event_captures_actor_and_redacts_detail(self):
        principal = security.Principal("p", "Ops", "operator", "AGENCY-A", permissions=security.ROLE_PERMISSIONS["operator"])
        event = security.audit_event(
            action="data:write",
            outcome="allowed",
            principal=principal,
            target="daily_ridership.csv",
            detail={"token": "abc", "rows": 3},
            method="POST",
            path="/api/data/upload",
            client="127.0.0.1",
            status_code=200,
        )
        self.assertEqual(event["role"], "operator")
        self.assertEqual(event["agency"], "AGENCY-A")
        self.assertEqual(event["detail"]["token"], security.REDACTED)
        self.assertEqual(event["detail"]["rows"], 3)

    def test_audit_append_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            for index in range(3):
                security.append_audit(path, {"action": f"a{index}", "outcome": "allowed"})
            with path.open("a", encoding="utf-8") as handle:
                handle.write("not json\n")
            events = security.read_audit(path, limit=10)
            self.assertEqual([event["action"] for event in events], ["a2", "a1", "a0"])

    def test_audit_trimming_keeps_recent_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            for index in range(50):
                security.append_audit(path, {"action": f"a{index}", "padding": "x" * 200})
            self.assertLess(path.stat().st_size, 5 * 1024 * 1024)
            self.assertIn("a49", path.read_text(encoding="utf-8"))


class ValidationTests(unittest.TestCase):
    def test_route_and_date_validation(self):
        self.assertEqual(security.validate_route_id(" R10 "), "R10")
        for bad in ("", "   ", "bad\x00route", "x" * 200):
            with self.assertRaises(security.InputValidationError):
                security.validate_route_id(bad)
        self.assertEqual(security.validate_iso_date("2025-06-01"), "2025-06-01")
        with self.assertRaises(security.InputValidationError):
            security.validate_iso_date("01/06/2025")
        with self.assertRaises(security.InputValidationError):
            security.validate_date_range("2025-06-10", "2025-06-01")

    def test_passenger_identifiers_are_refused(self):
        with self.assertRaises(security.InputValidationError) as context:
            security.ensure_no_passenger_identifiers({"records": [{"route_id": "R1", "passenger_id": "abc", "email": "a@b.c"}]})
        self.assertIn("passenger_id", context.exception.detail)
        self.assertIn("email", context.exception.detail)
        security.ensure_no_passenger_identifiers(
            {"records": [{"service_date": "2025-06-01", "route_id": "R1", "passengers": 120}]}
        )

    def test_agency_codes(self):
        self.assertEqual(security.validate_agency_code("AGENCY-A"), "AGENCY-A")
        for bad in ("", "-bad", "x", "bad\x00code", "a" * 80):
            with self.assertRaises(security.InputValidationError):
                security.validate_agency_code(bad)


class UploadTests(unittest.TestCase):
    def test_safe_names_only(self):
        self.assertEqual(security.validate_upload_name("ridership.csv"), "ridership.csv")
        for bad in (None, "", "../secret.csv", "folder/file.csv", "/etc/passwd.csv", ".hidden.csv", "file.exe", "file"):
            with self.assertRaises(security.InputValidationError, msg=str(bad)):
                security.validate_upload_name(bad)

    def test_payload_limits(self):
        with self.assertRaises(security.InputValidationError):
            security.validate_upload_payload(b"", filename="a.csv")
        with self.assertRaises(security.InputValidationError):
            security.validate_upload_payload(b"x" * 10, filename="a.csv", max_bytes=5)
        self.assertEqual(security.validate_upload_payload(b"a,b\n1,2\n", filename="a.csv"), "a.csv")

    def test_zip_extraction_stays_inside_destination(self):
        payload = build_zip({"agency.txt": b"agency_name\nMetro\n", "nested/routes.txt": b"route_id\nR1\n"})
        with tempfile.TemporaryDirectory() as tmp:
            written = security.safe_extract_zip(payload, tmp)
            self.assertEqual(sorted(path.name for path in written), ["agency.txt", "routes.txt"])
            self.assertEqual((Path(tmp) / "nested" / "routes.txt").read_bytes(), b"route_id\nR1\n")
            for path in written:
                self.assertTrue(str(path.resolve()).startswith(str(Path(tmp).resolve())))

    def test_zip_traversal_and_absolute_paths_rejected(self):
        with self.assertRaises(security.InputValidationError):
            security.safe_extract_zip(build_zip({"../escape.txt": b"x"}), tempfile.mkdtemp())
        with self.assertRaises(security.InputValidationError):
            security.safe_extract_zip(build_zip({"/etc/escape.txt": b"x"}), tempfile.mkdtemp())
        with self.assertRaises(security.InputValidationError):
            security.safe_extract_zip(build_zip({"a/../../escape.txt": b"x"}), tempfile.mkdtemp())

    def test_zip_symlink_rejected(self):
        payload = build_zip({"agency.txt": b"ok"}, symlinks=("link.txt",))
        with self.assertRaises(security.InputValidationError) as context:
            security.safe_extract_zip(payload, tempfile.mkdtemp())
        self.assertIn("Symlink", context.exception.detail)

    def test_zip_entry_and_size_limits(self):
        payload = build_zip({f"file{index}.txt": b"x" for index in range(4)})
        with self.assertRaises(security.InputValidationError):
            security.inspect_zip_payload(payload, max_entries=2)
        with self.assertRaises(security.InputValidationError):
            security.inspect_zip_payload(payload, max_uncompressed_bytes=2)
        with self.assertRaises(security.InputValidationError):
            security.inspect_zip_payload(payload, max_bytes=10)

    def test_zip_bomb_ratio_rejected(self):
        payload = build_zip({"bomb.txt": b"\x00" * (2 * 1024 * 1024)})
        with self.assertRaises(security.InputValidationError) as context:
            security.inspect_zip_payload(payload, max_ratio=50)
        self.assertIn("compression ratio", context.exception.detail)

    def test_invalid_zip_rejected(self):
        with self.assertRaises(security.InputValidationError):
            security.inspect_zip_payload(b"not a zip file")
        with self.assertRaises(security.InputValidationError):
            security.inspect_zip_payload(b"")


if __name__ == "__main__":
    unittest.main()
