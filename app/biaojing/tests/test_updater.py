from __future__ import annotations

import io
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from biaojing import updater


def bundle(version: str, filename: str = "module.py", value: str = "new") -> bytes:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("app/biaojing/__init__.py",
                         f'PRODUCT_NAME = "标镜"\nVERSION = "{version}"\n')
        files = {f"app/biaojing/{module}.py": "# test module\n"
                 for module in ("pdf_parser", "docx_parser", "xlsx_parser",
                                "money", "candidates", "rules", "workspace",
                                "webapp", "cli", "updater")}
        files[f"app/biaojing/{filename}"] = value
        for path, content in files.items():
            archive.writestr(path, content)
    return data.getvalue()


def application_bundle(version: str) -> bytes:
    source = Path(updater.__file__).parent
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in source.glob("*.py"):
            content = path.read_text(encoding="utf-8")
            if path.name == "__init__.py":
                content = content.replace(
                    f'VERSION = "{updater.VERSION}"',
                    f'VERSION = "{version}"')
            archive.writestr(f"app/biaojing/{path.name}", content)
    return data.getvalue()


def create_legacy_workspace(root: Path, malformed: bool = False) -> None:
    root.mkdir(parents=True)
    db = sqlite3.connect(root / "biaojing.sqlite3")
    if malformed:
        db.execute("CREATE TABLE confirmations(id INTEGER PRIMARY KEY)")
    else:
        db.executescript("""
            CREATE TABLE candidates(
                id INTEGER PRIMARY KEY,sha256 TEXT,field TEXT,value_json TEXT,
                evidence_id TEXT,locator_json TEXT,locator_display TEXT,note TEXT);
            CREATE TABLE confirmations(
                id INTEGER PRIMARY KEY,event_id TEXT,lot_id TEXT,bidder_id TEXT,
                field TEXT,value TEXT,evidence_id TEXT,original_candidate TEXT,
                action TEXT,confirmed_at TEXT,
                UNIQUE(event_id,lot_id,bidder_id,field));
            CREATE TABLE sources(
                sha256 TEXT PRIMARY KEY,doc_type TEXT,status TEXT,parser TEXT,
                counts_json TEXT,metadata_json TEXT,source_type TEXT,first_ref TEXT,
                imported_at TEXT);
            CREATE TABLE evidence_store(
                evidence_id TEXT PRIMARY KEY,sha256 TEXT,ordinal INTEGER,kind TEXT,
                locator_display TEXT,locator_json TEXT,quote TEXT,source_type TEXT);
            CREATE TABLE screen_runs(
                id INTEGER PRIMARY KEY,run_at TEXT,rules_version TEXT,
                facts_sha256 TEXT,facts_json TEXT,outcome_json TEXT);
            INSERT INTO confirmations(event_id,lot_id,bidder_id,field,value,
                evidence_id,action,confirmed_at)
                VALUES('OLD-EVENT','OLD-LOT','OLD-BIDDER','bidder_name',
                '旧版主体','E-OLD','confirm','2026-01-01');
        """)
    db.commit()
    db.close()


class UpdaterTests(unittest.TestCase):
    def test_version_tags_compare_without_v_prefix(self):
        self.assertEqual(updater.version_key("v0.2.1"), (0, 2, 1))
        self.assertGreater(updater.version_key("0.2.0"),
                           updater.version_key("0.1.99"))

    def test_unconfigured_update_is_a_noop(self):
        self.assertEqual(updater.check_and_stage_update(""), {
            "status": "unconfigured", "current_version": updater.VERSION})

    def test_github_release_asset_is_digest_verified_and_staged(self):
        archive = bundle("0.2.2")
        tag = "v0.2.2"
        api_payload = json.dumps({
            "tag_name": tag,
            "assets": [{
                "name": f"biaojing-source-{tag}.zip",
                "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
                "size": len(archive),
                "browser_download_url": (
                    "https://github.com/test/biaojing/releases/download/"
                    f"{tag}/biaojing-source-{tag}.zip"),
            }],
        }).encode()

        class Response:
            def __init__(self, body, final_url):
                self.body, self.final_url = body, final_url
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, limit): return self.body
            def geturl(self): return self.final_url

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            replies = iter([
                Response(api_payload,
                         "https://api.github.com/repos/test/biaojing/releases/latest"),
                Response(archive,
                         "https://release-assets.githubusercontent.com/biaojing.zip"),
            ])
            with patch.object(updater, "_project_root", return_value=root), \
                    patch.object(updater, "urlopen", side_effect=lambda *a, **k: next(replies)):
                result = updater.check_and_stage_update("test/biaojing")
            self.assertEqual(result["status"], "staged")
            self.assertEqual(result["latest_version"], tag)
            self.assertEqual(updater._pending(root)["version"], tag)

    def test_staged_update_replaces_only_application_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "app" / "biaojing"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text('VERSION = "0.1.0"\n')
            (package / "old.py").write_text("old")
            workspace = root / "user-workspace"
            workspace.mkdir()
            (workspace / "source.pdf").write_bytes(b"keep")

            updater._stage(root, bundle("0.2.1"), "v0.2.1")
            self.assertEqual(updater.apply_pending_update(root), "v0.2.1")
            self.assertTrue((package / "module.py").is_file())
            self.assertFalse((package / "old.py").exists())
            self.assertEqual((workspace / "source.pdf").read_bytes(), b"keep")

    def test_update_preflights_legacy_database_copy_without_modifying_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "app" / "biaojing"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text('VERSION = "0.1.0"\n')
            legacy = root / "legacy-workspace"
            create_legacy_workspace(legacy)

            updater._stage(root, application_bundle("0.2.1"), "v0.2.1")
            self.assertEqual(updater.apply_pending_update(root, legacy), "v0.2.1")

            original = sqlite3.connect(legacy / "biaojing.sqlite3")
            try:
                columns = {row[1] for row in original.execute(
                    "PRAGMA table_info(confirmations)")}
                self.assertNotIn("source_role", columns)
                self.assertEqual(original.execute(
                    "SELECT value FROM confirmations").fetchone()[0], "旧版主体")
            finally:
                original.close()

    def test_migration_preflight_failure_rolls_back_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "app" / "biaojing"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text('VERSION = "0.1.0"\n')
            (package / "old.py").write_text("old")
            malformed = root / "malformed-workspace"
            create_legacy_workspace(malformed, malformed=True)

            updater._stage(root, application_bundle("0.2.1"), "v0.2.1")
            with self.assertRaises(updater.UpdateError):
                updater.apply_pending_update(root, malformed)

            self.assertEqual((package / "__init__.py").read_text(),
                             'VERSION = "0.1.0"\n')
            self.assertTrue((package / "old.py").is_file())
            self.assertEqual(updater._rejected_update(root)["version"], "v0.2.1")
            original = sqlite3.connect(malformed / "biaojing.sqlite3")
            try:
                self.assertEqual([row[1] for row in original.execute(
                    "PRAGMA table_info(confirmations)")], ["id"])
            finally:
                original.close()

    def test_import_check_failure_rolls_back_and_remembers_bad_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "app" / "biaojing"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text('VERSION = "0.1.0"\n')
            (package / "old.py").write_text("old")
            updater._stage(root, bundle("0.2.1", "rules.py", "def broken(:\n"),
                            "v0.2.1")
            with self.assertRaises(updater.UpdateError):
                updater.apply_pending_update(root)
            self.assertEqual((package / "__init__.py").read_text(),
                             'VERSION = "0.1.0"\n')
            self.assertTrue((package / "old.py").is_file())
            self.assertEqual(updater._rejected_update(root)["version"], "v0.2.1")
            self.assertFalse((root / ".biaojing-update-staging").exists())

    def test_bundle_rejects_path_traversal_without_touching_current_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "app" / "biaojing"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text('VERSION = "0.1.0"\n')
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as bundle_file:
                bundle_file.writestr("app/biaojing/__init__.py",
                                     'VERSION = "0.2.0"\n')
                bundle_file.writestr("../../outside.txt", "bad")
            with self.assertRaises(updater.UpdateError):
                updater._stage(root, archive.getvalue(), "v0.2.0")
            self.assertEqual((package / "__init__.py").read_text(),
                             'VERSION = "0.1.0"\n')
            self.assertFalse((root / "outside.txt").exists())


if __name__ == "__main__":
    unittest.main()
