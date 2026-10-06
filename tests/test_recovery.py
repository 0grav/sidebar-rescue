"""Synthetic checks only: never read a real Codex home."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import restore_projects as recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sidebar-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.home = (Path(self.temp.name) / "example home # fixture").resolve()
        self.home.mkdir()
        self.env = mock.patch.dict(os.environ, {"CODEX_SQLITE_HOME": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db_path = self.home / "state_5.sqlite"
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.executescript("""
                CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, position INTEGER,
                    created_at_ms INTEGER, updated_at_ms INTEGER);
                CREATE TABLE project_roots (project_id TEXT, path TEXT);
                CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT, project_id TEXT, body TEXT);
            """)
            db.execute("INSERT INTO projects VALUES (?, ?, ?, ?, ?)", ("project-one", "Example Alpha", 0, 1, 2))
            db.executemany("INSERT INTO project_roots VALUES (?, ?)", [
                ("project-one", r"C:\example\alpha"), ("project-one", r"C:\example\alpha-extra")])
            db.executemany("INSERT INTO threads VALUES (?, ?, ?, ?)", [
                ("thread-one", r"C:\example\alpha", "project-one", "SYNTHETIC_BODY"),
                ("thread-two", r"C:\example\alpha-extra", None, "SYNTHETIC_BODY"),
                ("thread-subdir", r"C:\example\alpha\child", None, "SYNTHETIC_BODY")])
        self.state_path = self.home / ".codex-global-state.json"
        self.state = {"unrelated": {"keep": True}, "projectless-thread-ids": [],
                      "app-server-projects-migration-by-host": {"example": {
                          "projectsMigrated": True, "threadAssignmentsMigrated": False,
                          "pendingThreadAssignmentIds": ["thread-two"]}}}
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        self.projects, self.threads = recovery.read_database(self.db_path)
        self.host = "local:" + str(self.home)

    def plan(self, state=None, snapshots=None, infer=False):
        return recovery.build_plan(self.state if state is None else state, self.projects, self.threads,
                                   snapshots or [], self.host, infer)

    def snapshot(self, legacy="legacy-one"):
        return {"local-projects": {legacy: {"id": legacy, "rootPaths": [r"C:\example\alpha"]}},
                "thread-project-assignments": {"thread-two": {"projectKind": "local", "projectId": legacy},
                                               "deleted-thread": {"projectKind": "local", "projectId": legacy}}}

    def apply(self, result):
        original = self.state_path.read_bytes()
        with mock.patch.object(recovery, "ensure_closed"), contextlib.redirect_stdout(io.StringIO()):
            recovery.apply_plan(self.state_path, original, result, self.db_path,
                                recovery.database_signature(self.db_path))
        return original

    def test_multiple_roots_preserved_and_inference_is_opt_in(self):
        result, sources = self.plan()
        self.assertEqual(len(result["local-projects"]["project-one"]["rootPaths"]), 2)
        self.assertEqual(sources["database"], 1)
        self.assertNotIn("thread-two", result["thread-project-assignments"])
        result, sources = self.plan(infer=True)
        self.assertEqual(sources["cwd"], 1)
        self.assertIn("thread-two", result["thread-project-assignments"])
        self.assertNotIn("thread-subdir", result["thread-project-assignments"])

    def test_shared_root_inference_is_skipped(self):
        self.projects["project-two"] = dict(self.projects["project-one"], id="project-two")
        result, sources = self.plan(infer=True)
        self.assertEqual(sources["ambiguous"], 1)
        self.assertNotIn("thread-two", result["thread-project-assignments"])

    def test_snapshots_restore_only_present_threads(self):
        result, sources = self.plan(snapshots=[self.snapshot()])
        self.assertEqual(sources["snapshot"], 1)
        self.assertEqual(result["thread-project-assignments"]["thread-two"]["projectId"], "legacy-one")
        self.assertNotIn("deleted-thread", result["thread-project-assignments"])

    def test_old_alias_resolves_to_current_project_id(self):
        state = self.snapshot("current-one")
        state["thread-project-assignments"] = {}
        result, _ = self.plan(state=state, snapshots=[self.snapshot("older-one")])
        self.assertEqual(result["thread-project-assignments"]["thread-two"]["projectId"], "current-one")
        self.assertNotIn("older-one", result["local-projects"])
        self.assertEqual(len(result["local-projects"]["current-one"]["rootPaths"]), 2)

    def test_remote_and_existing_links_and_settings_are_preserved(self):
        state = copy.deepcopy(self.state)
        state["thread-project-assignments"] = {"thread-one": {"projectKind": "cloud", "projectId": "cloud-example"}}
        state["thread-project-membership-host-ids"] = {"thread-one": "remote-example", "thread-two": "remote-example"}
        state["local-projects"] = {"other-project": {"rootPaths": [r"C:\other-example"]}}
        state["project-order"] = ["other-project"]
        state["app-server-project-id-by-legacy-project-id-by-host"] = {"remote-example": {"other-project": "other-id"}}
        before = copy.deepcopy(state)
        result, _ = self.plan(state=state, infer=True)
        self.assertEqual(result["thread-project-assignments"], before["thread-project-assignments"])
        self.assertEqual(result["thread-project-membership-host-ids"], before["thread-project-membership-host-ids"])
        for key in ("unrelated", "app-server-projects-migration-by-host", "app-server-project-id-by-legacy-project-id-by-host"):
            self.assertEqual(result[key], before[key])
        self.assertEqual(state, before)
        self.assertEqual(result["project-order"][0], "other-project")

    def test_explicit_projectless_prevents_reassignment(self):
        state = dict(self.state, **{"projectless-thread-ids": ["thread-one", "thread-two"]})
        result, _ = self.plan(state=state, snapshots=[self.snapshot()], infer=True)
        self.assertFalse(result["thread-project-assignments"])

    def test_newer_historical_removal_blocks_older_link(self):
        result, sources = self.plan(snapshots=[{"projectless-thread-ids": ["thread-two"]}, self.snapshot()], infer=True)
        self.assertNotIn("thread-two", result["thread-project-assignments"])
        self.assertEqual(sources["cwd"], 0)

    def test_foreign_snapshot_mapping_is_not_treated_as_local(self):
        snapshot = self.snapshot()
        snapshot["app-server-project-id-by-legacy-project-id-by-host"] = {"remote-example": {"legacy-one": "project-one"}}
        result, sources = self.plan(snapshots=[snapshot])
        self.assertEqual(sources["snapshot"], 0)
        self.assertNotIn("thread-two", result["thread-project-assignments"])

    def test_host_path_case_and_slash_normalization(self):
        snapshot = self.snapshot()
        snapshot["local-projects"] = {}
        snapshot["app-server-project-id-by-legacy-project-id-by-host"] = {
            "local:" + str(self.home).upper().replace("\\", "/"): {"legacy-one": "project-one"}}
        result, _ = self.plan(snapshots=[snapshot])
        self.assertIn("legacy-one", result["local-projects"])

    def test_path_normalization_keeps_drive_root(self):
        self.assertEqual(recovery.normalized(r"\\?\C:\example\alpha"), recovery.normalized("c:/example/alpha/"))
        self.assertNotEqual(recovery.normalized("C:\\"), recovery.normalized("C:"))

    def test_invalid_snapshot_formats_are_skipped(self):
        for index, raw in enumerate((b"not JSON", b"[]", b'{"thread-project-assignments":{"x":null}}')):
            (self.home / f"..codex-global-state.json.tmp-{index}").write_bytes(raw)
        self.assertEqual(recovery.load_snapshots(self.home), [])

    def test_backup_snapshot_is_discovered(self):
        (self.home / ".codex-global-state.json.bak").write_text(json.dumps(self.snapshot()), encoding="utf-8")
        self.assertEqual(len(recovery.load_snapshots(self.home)), 1)

    def test_unsupported_global_state_and_nan_are_rejected(self):
        for value in ({"local-projects": []}, {"project-order": [1]}, {"thread-project-assignments": {"x": None}}):
            with self.assertRaises(recovery.RecoveryError):
                recovery.validate_state(value)
        with self.assertRaises(ValueError):
            recovery.json_object(b'{"value": NaN}')
        with self.assertRaises(ValueError):
            recovery.json_object(b'{"duplicate":1,"duplicate":2}')

    def test_database_read_is_read_only_and_supports_escaped_uri(self):
        before = self.db_path.read_bytes()
        recovery.read_database(self.db_path)
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertNotIn("body", self.threads[0])

    def test_unsupported_schema_is_rejected(self):
        path = self.home / "unsupported.sqlite"
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE threads (id TEXT)")
        with self.assertRaises(recovery.RecoveryError):
            recovery.read_database(path)

    def test_unknown_sqlite_project_reference_is_rejected(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE threads SET project_id=? WHERE id=?", ("missing-project", "thread-one"))
        with self.assertRaises(recovery.RecoveryError):
            recovery.read_database(self.db_path)

    def test_database_detection_refuses_ambiguous_locations(self):
        nested = self.home / "sqlite"
        nested.mkdir()
        (nested / "state_5.sqlite").write_bytes(b"synthetic")
        with self.assertRaises(recovery.RecoveryError):
            recovery.locate_database(self.home)
        self.assertEqual(recovery.locate_database(self.home, self.home), self.db_path)

    def test_database_detection_finds_nested_location(self):
        nested = self.home / "sqlite"
        nested.mkdir()
        self.db_path.rename(nested / "state_5.sqlite")
        self.assertEqual(recovery.locate_database(self.home), nested / "state_5.sqlite")

    def test_config_environment_disagreement_requires_explicit_selection(self):
        (self.home / "config.toml").write_text("sqlite_home = " + json.dumps(str(self.home)), encoding="utf-8")
        with mock.patch.dict(os.environ, {"CODEX_SQLITE_HOME": str(self.home / "different-example")}):
            with self.assertRaises(recovery.RecoveryError):
                recovery.locate_database(self.home)
        self.assertEqual(recovery.locate_database(self.home), self.db_path)

    @unittest.skipUnless(os.name == "nt", "Windows process checker")
    def test_running_process_and_failed_process_check_are_rejected(self):
        result = subprocess.CompletedProcess([], 0, '"ChatGPT.exe","123","Console","1","100 K"\n')
        with mock.patch.object(recovery.subprocess, "run", return_value=result):
            with self.assertRaises(recovery.RecoveryError):
                recovery.ensure_closed()
        with mock.patch.object(recovery.subprocess, "run", side_effect=subprocess.TimeoutExpired("tasklist", 15)):
            with self.assertRaises(recovery.RecoveryError):
                recovery.ensure_closed()

    def test_apply_backs_up_exact_bytes_and_keeps_sqlite_unchanged(self):
        result, _ = self.plan()
        database_before = self.db_path.read_bytes()
        original = self.apply(result)
        backups = list(self.home.glob(".codex-global-state.json.backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8")), result)
        self.assertEqual(self.db_path.read_bytes(), database_before)
        self.assertFalse(list(self.home.glob(".codex-sidebar-recovery-*")))
        self.assertFalse((self.home / ".codex-sidebar-recovery.lock").exists())

    def test_changed_state_is_not_overwritten(self):
        result, _ = self.plan()
        original = self.state_path.read_bytes()
        self.state_path.write_bytes(b'{"changed":true}')
        with mock.patch.object(recovery, "ensure_closed"), self.assertRaises(recovery.RecoveryError):
            recovery.apply_plan(self.state_path, original, result, self.db_path, recovery.database_signature(self.db_path))
        self.assertEqual(self.state_path.read_bytes(), b'{"changed":true}')
        self.assertFalse(list(self.home.glob(".codex-global-state.json.backup-*")))

    def test_changed_database_is_not_used_for_apply(self):
        signature = recovery.database_signature(self.db_path)
        with contextlib.closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE threads SET cwd=? WHERE id=?", ("changed-example", "thread-one"))
        original = self.state_path.read_bytes()
        with mock.patch.object(recovery, "ensure_closed"), self.assertRaises(recovery.RecoveryError):
            recovery.apply_plan(self.state_path, original, self.plan()[0], self.db_path, signature)
        self.assertEqual(self.state_path.read_bytes(), original)

    def test_atomic_replace_failure_keeps_original_and_backup(self):
        original = self.state_path.read_bytes()
        with mock.patch.object(recovery.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                self.apply(self.plan()[0])
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertEqual(len(list(self.home.glob(".codex-global-state.json.backup-*"))), 1)
        self.assertFalse(list(self.home.glob(".codex-sidebar-recovery-*")))

    def test_process_started_before_replace_aborts_write(self):
        original = self.state_path.read_bytes()
        with mock.patch.object(recovery, "ensure_closed", side_effect=[None, recovery.RecoveryError("synthetic process")]):
            with self.assertRaises(recovery.RecoveryError):
                recovery.apply_plan(self.state_path, original, self.plan()[0], self.db_path, recovery.database_signature(self.db_path))
        self.assertEqual(self.state_path.read_bytes(), original)

    def test_lock_refuses_second_writer_and_remains_untouched(self):
        lock = self.home / ".codex-sidebar-recovery.lock"
        lock.write_text("synthetic lock", encoding="utf-8")
        with self.assertRaises(recovery.RecoveryError):
            self.apply(self.plan()[0])
        self.assertEqual(lock.read_text(encoding="utf-8"), "synthetic lock")

    def test_plan_is_idempotent(self):
        result, _ = self.plan(infer=True)
        again, sources = self.plan(state=result, infer=True)
        self.assertEqual(result, again)
        self.assertEqual(sum(sources.values()), 0)

    @unittest.skipUnless(os.name == "nt", "Windows CLI")
    def test_preview_preserves_files_and_hides_project_details(self):
        original, database = self.state_path.read_bytes(), self.db_path.read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            recovery.main(["--codex-home", str(self.home)])
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertEqual(self.db_path.read_bytes(), database)
        self.assertNotIn("Example Alpha", output.getvalue())
        self.assertNotIn("example\\alpha", output.getvalue())
        self.assertNotIn("SYNTHETIC_BODY", output.getvalue())
        self.assertFalse(list(self.home.glob(".codex-global-state.json.backup-*")))


    def test_wal_records_are_read_without_ignoring_sidecar(self):
        with contextlib.closing(sqlite3.connect(self.db_path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO threads VALUES (?, ?, ?, ?)",
                       ("thread-wal", r"C:\example\alpha", "project-one", "SYNTHETIC_BODY"))
            db.commit()
            before = self.db_path.read_bytes()
            _, threads = recovery.read_database(self.db_path)
            self.assertIn("thread-wal", {thread["id"] for thread in threads})
            self.assertEqual(self.db_path.read_bytes(), before)

    def test_bak_file_is_not_modified(self):
        bak = self.home / ".codex-global-state.json.bak"
        bak.write_bytes(b"synthetic original backup")
        self.apply(self.plan()[0])
        self.assertEqual(bak.read_bytes(), b"synthetic original backup")

    @unittest.skipUnless(os.name == "nt", "Windows CLI")
    def test_apply_cli_uses_only_synthetic_files(self):
        output = io.StringIO()
        with mock.patch.object(recovery, "ensure_closed"), contextlib.redirect_stdout(output):
            recovery.main(["--codex-home", str(self.home), "--apply"])
        self.assertEqual(len(list(self.home.glob(".codex-global-state.json.backup-*"))), 1)
        self.assertNotIn("Example Alpha", output.getvalue())

    @unittest.skipUnless(os.name == "nt", "Windows CLI")
    def test_apply_cli_refuses_running_process_before_writing(self):
        original = self.state_path.read_bytes()
        with mock.patch.object(recovery, "ensure_closed", side_effect=recovery.RecoveryError("synthetic process")):
            with self.assertRaises(recovery.RecoveryError):
                recovery.main(["--codex-home", str(self.home), "--apply"])
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertFalse(list(self.home.glob(".codex-global-state.json.backup-*")))


if __name__ == "__main__":
    unittest.main()

