"""Experimental Windows helper for restoring local Codex sidebar metadata."""

import argparse
import copy
import csv
import datetime as dt
import json
import ntpath
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import uuid

try:
    import tomllib
except ModuleNotFoundError:
    raise SystemExit("Python 3.11 or newer is required.")


class RecoveryError(Exception):
    """An unsafe or unsupported recovery condition."""


def normalized(path):
    if not path:
        return ""
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return ntpath.normcase(ntpath.normpath(path))


def json_object(raw):
    def reject_constant(value):
        raise ValueError("Non-standard JSON constant")
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    value = json.loads(raw.decode("utf-8-sig"), parse_constant=reject_constant, object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def validate_state(state):
    for key in ("local-projects", "thread-project-assignments", "thread-project-membership-host-ids",
                "app-server-project-id-by-legacy-project-id-by-host", "app-server-projects-migration-by-host"):
        if key in state and state[key] is not None and not isinstance(state[key], dict):
            raise RecoveryError("Unsupported global-state format; no changes made.")
    for key in ("project-order", "projectless-thread-ids"):
        if key in state and (not isinstance(state[key], list) or
                             any(not isinstance(value, str) for value in state[key])):
            raise RecoveryError("Unsupported global-state list; no changes made.")
    for item in (state.get("local-projects") or {}).values():
        if not isinstance(item, dict) or not isinstance(item.get("rootPaths"), list) or \
                any(not isinstance(root, str) for root in item["rootPaths"]):
            raise RecoveryError("Unsupported project metadata; no changes made.")
    for link in (state.get("thread-project-assignments") or {}).values():
        if not isinstance(link, dict) or not isinstance(link.get("projectId"), str):
            raise RecoveryError("Unsupported thread metadata; no changes made.")


def ensure_closed():
    if os.name != "nt":
        raise RecoveryError("Writing is supported only on Windows.")
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        raise RecoveryError("Cannot locate the Windows process checker; no changes made.")
    try:
        result = subprocess.run([str(Path(system_root) / "System32" / "tasklist.exe"),
                                 "/FO", "CSV", "/NH"], capture_output=True, text=True,
                                timeout=15, check=True)
    except (OSError, subprocess.SubprocessError):
        raise RecoveryError("Could not verify running processes; no changes made.") from None
    rows = list(csv.reader(result.stdout.splitlines()))
    if not rows or any(len(row) < 2 for row in rows):
        raise RecoveryError("Process check returned an unexpected response; no changes made.")
    names = {row[0].lower() for row in rows}
    if names & {"codex.exe", "chatgpt.exe", "codex-app.exe"}:
        raise RecoveryError("Quit Codex Desktop and Codex CLI, including background processes, then retry.")


def locate_database(home, sqlite_home=None):
    if sqlite_home is not None:
        directory = sqlite_home.expanduser().resolve()
    else:
        configured = None
        config_path = home / "config.toml"
        if config_path.is_file():
            try:
                configured = tomllib.loads(config_path.read_text(encoding="utf-8-sig")).get("sqlite_home")
            except (OSError, UnicodeError, ValueError):
                raise RecoveryError("Cannot parse config.toml; pass --sqlite-home explicitly.") from None
        env_home = os.environ.get("CODEX_SQLITE_HOME")
        if configured and not isinstance(configured, str):
            raise RecoveryError("Invalid sqlite_home setting; pass --sqlite-home explicitly.")
        if configured and env_home and normalized(configured) != normalized(env_home):
            raise RecoveryError("Config and environment select different databases; pass --sqlite-home explicitly.")
        selected = configured or env_home
        if selected:
            if not Path(selected).expanduser().is_absolute():
                raise RecoveryError("SQLite home must be an absolute directory; pass --sqlite-home explicitly.")
            directory = Path(selected).expanduser().resolve()
        else:
            found = {candidate.resolve() for candidate in (home, home / "sqlite")
                     if (candidate / "state_5.sqlite").is_file()}
            if len(found) != 1:
                raise RecoveryError("No unique state_5.sqlite found; pass --sqlite-home explicitly.")
            directory = found.pop()
    path = directory / "state_5.sqlite"
    if not path.is_file():
        raise RecoveryError("state_5.sqlite was not found in the selected directory.")
    return path


def database_signature(path):
    signature = []
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            info = candidate.stat()
            signature.append((info.st_size, info.st_mtime_ns, info.st_ino))
        except FileNotFoundError:
            signature.append(None)
    return signature


def read_database(path):
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            required = {"projects": {"id", "name", "position", "created_at_ms", "updated_at_ms"},
                        "project_roots": {"project_id", "path"}, "threads": {"id", "cwd", "project_id"}}
            for table, columns in required.items():
                found = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                if not columns <= found:
                    raise RecoveryError("Unsupported SQLite schema; no changes made.")
            if [row[0] for row in db.execute("PRAGMA integrity_check")] != ["ok"]:
                raise RecoveryError("SQLite integrity check failed; no changes made.")
            projects = {row["id"]: dict(row, rootPaths=[]) for row in db.execute(
                "SELECT id, name, position, created_at_ms, updated_at_ms FROM projects ORDER BY position, id")}
            for row in db.execute("SELECT project_id, path FROM project_roots ORDER BY project_id, path"):
                if row["project_id"] not in projects or not isinstance(row["path"], str) or not row["path"]:
                    raise RecoveryError("Inconsistent project roots; no changes made.")
                roots = projects[row["project_id"]]["rootPaths"]
                if row["path"] not in roots:
                    roots.append(row["path"])
            threads = [dict(row) for row in db.execute("SELECT id, cwd, project_id FROM threads")]
    except sqlite3.Error:
        raise RecoveryError("Could not read SQLite safely; no changes made.") from None
    finally:
        if "db" in locals():
            db.close()
    if not projects or any(not project["rootPaths"] for project in projects.values()):
        raise RecoveryError("No recoverable projects, or a project has no roots; no changes made.")
    if any(not isinstance(pid, str) or not pid or not isinstance(project["name"], str) or
           any(type(project[key]) is not int for key in ("position", "created_at_ms", "updated_at_ms"))
           for pid, project in projects.items()):
        raise RecoveryError("Unsupported project records; no changes made.")
    if any(not isinstance(thread["id"], str) or not isinstance(thread["cwd"], (str, type(None))) for thread in threads):
        raise RecoveryError("Unsupported thread records; no changes made.")
    if any(thread["project_id"] is not None and thread["project_id"] not in projects for thread in threads):
        raise RecoveryError("A thread refers to an unknown project; no changes made.")
    return projects, threads


def load_snapshots(home):
    paths = set(home.glob("..codex-global-state.json*")) | set(home.glob(".codex-global-state.json.bak*"))
    paths |= set(home.glob(".codex-global-state.json.backup-*"))
    saved = []
    for path in sorted(paths, key=lambda item: item.stat().st_mtime_ns, reverse=True):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            state = json_object(path.read_bytes())
            validate_state(state)
            saved.append(state)
        except (OSError, UnicodeError, ValueError, RecoveryError):
            continue
    return saved


def legacy_mapping(state, projects, host):
    all_maps = state.get("app-server-project-id-by-legacy-project-id-by-host") or {}
    same_host = lambda key: key.startswith("local:") and normalized(key[6:]) == normalized(host[6:])
    host_map = {}
    for key, values in all_maps.items():
        if same_host(key):
            if not isinstance(values, dict):
                raise RecoveryError("Unsupported host mapping; no changes made.")
            for legacy, server in values.items():
                if legacy in host_map and host_map[legacy] != server:
                    raise RecoveryError("Conflicting host mappings; no changes made.")
                host_map[legacy] = server
    mapping = {legacy: server for legacy, server in host_map.items() if isinstance(server, str) and server in projects}
    foreign_ids = {legacy for key, values in all_maps.items()
                   if not same_host(key) and isinstance(values, dict) for legacy in values}
    for legacy, item in (state.get("local-projects") or {}).items():
        if legacy in foreign_ids and legacy not in host_map:
            continue
        matches = {server for server, project in projects.items()
                   if {normalized(root) for root in item["rootPaths"]} &
                   {normalized(root) for root in project["rootPaths"]}}
        if legacy in mapping and matches and mapping[legacy] not in matches:
            raise RecoveryError("Conflicting project IDs and roots; no changes made.")
        if legacy not in mapping and len(matches) == 1:
            mapping[legacy] = matches.pop()
    return mapping


def build_plan(state, projects, threads, snapshots, host, infer=False):
    result = copy.deepcopy(state)
    candidates = [state] + snapshots
    mappings = [legacy_mapping(candidate, projects, host) for candidate in candidates]
    local_projects = result.get("local-projects") or {}
    legacy_for_server = {}
    for server in projects:
        existing = [legacy for legacy, pid in mappings[0].items() if pid == server and legacy in local_projects]
        if len(existing) > 1:
            raise RecoveryError("Multiple existing project IDs match one SQLite project; no changes made.")
        if existing:
            legacy_for_server[server] = existing[0]
    for mapping in mappings:
        for legacy, server in mapping.items():
            legacy_for_server.setdefault(server, legacy)
    for server in projects:
        legacy_for_server.setdefault(server, server)
    if len(set(legacy_for_server.values())) != len(projects):
        raise RecoveryError("Project IDs are ambiguous; no changes made.")
    for server, project in projects.items():
        legacy = legacy_for_server[server]
        if legacy in local_projects and mappings[0].get(legacy) != server:
            raise RecoveryError("A recovered ID conflicts with an existing project; no changes made.")
        if legacy not in local_projects:
            local_projects[legacy] = {"id": legacy, "name": project["name"], "rootPaths": project["rootPaths"],
                                      "createdAt": project["created_at_ms"], "updatedAt": project["updated_at_ms"]}
        else:
            known = {normalized(root) for root in local_projects[legacy]["rootPaths"]}
            local_projects[legacy]["rootPaths"].extend(root for root in project["rootPaths"] if normalized(root) not in known)

    assignments = result.get("thread-project-assignments") or {}
    memberships = result.get("thread-project-membership-host-ids") or {}
    projectless = set(result.get("projectless-thread-ids", []))
    sources = {"database": 0, "snapshot": 0, "cwd": 0, "ambiguous": 0}
    for thread in threads:
        tid, server = thread["id"], thread["project_id"]
        if tid in projectless:
            continue
        if tid in assignments or memberships.get(tid) not in (None, "local"):
            continue
        source, blocked = "database", False
        if server is None:
            for candidate, mapping in zip(candidates, mappings):
                if tid in candidate.get("projectless-thread-ids", []):
                    blocked = True
                    break
                link = (candidate.get("thread-project-assignments") or {}).get(tid)
                membership = (candidate.get("thread-project-membership-host-ids") or {}).get(tid)
                if link and link.get("projectKind") == "local" and membership in (None, "local"):
                    server = mapping.get(link["projectId"])
                    if server is not None:
                        source = "snapshot"
                        break
        if blocked:
            continue
        if server is None and infer:
            matches = [pid for pid, project in projects.items()
                       if normalized(thread["cwd"]) in {normalized(root) for root in project["rootPaths"]}]
            if len(matches) > 1:
                sources["ambiguous"] += 1
            elif matches:
                server, source = matches[0], "cwd"
        if server is not None:
            assignments[tid] = {"projectKind": "local", "projectId": legacy_for_server[server]}
            memberships[tid] = "local"
            sources[source] += 1
    result["local-projects"] = local_projects
    result["thread-project-assignments"] = assignments
    result["thread-project-membership-host-ids"] = memberships
    order = list(dict.fromkeys(result.get("project-order", [])))
    result["project-order"] = order + [legacy for legacy in legacy_for_server.values() if legacy not in order]
    # Do not claim that app-server migrations have completed: leave migration
    # flags, pending IDs and mapping registries unchanged.
    return result, sources


def apply_plan(state_path, original, replacement, db_path, signature):
    lock = state_path.with_name(".codex-sidebar-recovery.lock")
    temp = None
    try:
        with lock.open("xb"):
            pass
    except FileExistsError:
        raise RecoveryError("Another recovery may be in progress; inspect the recovery lock before retrying.") from None
    try:
        ensure_closed()
        if state_path.is_symlink() or state_path.read_bytes() != original or database_signature(db_path) != signature:
            raise RecoveryError("State changed during recovery, or is a symbolic link; no changes made.")
        encoded = (json.dumps(replacement, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = state_path.with_name(state_path.name + ".backup-" + stamp + "-" + uuid.uuid4().hex[:8])
        with backup.open("xb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        descriptor, name = tempfile.mkstemp(prefix=".codex-sidebar-recovery-", suffix=".tmp", dir=state_path.parent)
        temp = Path(name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        ensure_closed()
        if state_path.read_bytes() != original or database_signature(db_path) != signature:
            raise RecoveryError("State changed before writing; original state was kept.")
        os.replace(temp, state_path)
        print("Sidebar metadata written. Backup file: " + backup.name)
        print("Restart Codex and check the sidebar. This does not confirm an app-level fix.")
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--sqlite-home", type=Path, help="Directory containing the active state_5.sqlite database.")
    parser.add_argument("--apply", action="store_true", help="Back up and write the global-state JSON; default is preview.")
    parser.add_argument("--infer-from-cwd", action="store_true", help="Opt in to exact cwd/root inference for unlinked chats.")
    parser.add_argument("--details", action="store_true", help="Show private local project names and roots in the terminal.")
    args = parser.parse_args(argv)
    if os.name != "nt":
        raise RecoveryError("This helper supports Windows only.")
    if args.apply:
        ensure_closed()
    home = args.codex_home.expanduser().resolve()
    state_path = home / ".codex-global-state.json"
    if not state_path.is_file() or state_path.is_symlink():
        raise RecoveryError("A regular global-state JSON file is required; pass --codex-home if needed.")
    try:
        original = state_path.read_bytes()
        state = json_object(original)
    except (OSError, UnicodeError, ValueError):
        raise RecoveryError("Cannot read valid global-state JSON; no changes made.") from None
    validate_state(state)
    db_path = locate_database(home, args.sqlite_home)
    signature = database_signature(db_path)
    projects, threads = read_database(db_path)
    if database_signature(db_path) != signature:
        raise RecoveryError("Database changed while reading; close Codex and retry.")
    replacement, sources = build_plan(state, projects, threads, load_snapshots(home), "local:" + str(home), args.infer_from_cwd)
    print(f"Recoverable SQLite projects: {len(projects)}; chats inspected: {len(threads)}")
    print(f"New links: {sum(sources[k] for k in ('database', 'snapshot', 'cwd'))} "
          f"(database: {sources['database']}, snapshot: {sources['snapshot']}, cwd inference: {sources['cwd']})")
    if sources["ambiguous"]:
        print(f"Ambiguous cwd matches skipped: {sources['ambiguous']}")
    if args.details:
        for project in projects.values():
            print(json.dumps({"name": project["name"], "roots": project["rootPaths"]}, ensure_ascii=True))
    if replacement == state:
        print("No metadata changes needed; no backup or write performed.")
    elif args.apply:
        apply_plan(state_path, original, replacement, db_path, signature)
    else:
        print("Preview only; global-state and database contents were not modified. Use --apply to write.")


if __name__ == "__main__":
    try:
        main()
    except RecoveryError as error:
        print("Error: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, TypeError):
        print("Error: local files could not be handled safely; stop and check permissions or format.", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)

