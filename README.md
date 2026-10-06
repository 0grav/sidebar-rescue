# Sidebar Rescue

An **experimental Windows helper** for the situation where local Codex projects disappear from the sidebar but their records survive in `state_5.sqlite`. Python 3.11+; standard library only; no third-party packages or network access required.

This is a community recovery aid, not an OpenAI-supported fix. It reconstructs **global-state JSON metadata only**. It does not repair SQLite, restore deleted chats, or confirm that a particular Desktop version will accept the reconstructed sidebar. Related reports: [#42739](https://github.com/openai/codex/issues/42739), [#42867](https://github.com/openai/codex/issues/42867).

## Run

Quit Codex Desktop and Codex CLI completely, including background processes. Keep them closed throughout recovery. In a terminal opened in this folder:

```powershell
python restore_projects.py
```

This previews counts without changing global-state or database contents. For a local review of project names and roots, use `--details`; that output is private. To back up and write the proposed metadata:

```powershell
python restore_projects.py --apply
```

Restart Codex and check the sidebar. A successful file write does not prove the app issue is fixed. If the sidebar remains empty or Codex resets the metadata again, restore the backup and report the Desktop version and counts, without uploading state files.

## Finding the database

The helper uses `CODEX_HOME` or the current user's `.codex` directory. It reads the top-level `sqlite_home` setting in `config.toml` and `CODEX_SQLITE_HOME`, then checks the Codex home and its `sqlite` subdirectory. It refuses to choose automatically if these locations are ambiguous. It does not scan the whole disk or reproduce all of Codex's layered/profile configuration.

Custom paths can be supplied without another chat:

```powershell
python restore_projects.py --codex-home 'C:\path\to\codex-home' --sqlite-home 'C:\path\to\sqlite-home'
```

The selected directory must contain the **active** `state_5.sqlite` and its normal SQLite sidecars, if present. The Codex home must contain valid `.codex-global-state.json`. The database must retain project and root records. A separate SQLite snapshot is not required when the active database still has those records. This script does not locate or restore SQLite backups; if the database records are gone, stop and use a separate recovery procedure. Do not replace the live database just to try this helper.

Only the known `state_5.sqlite` table layout is supported. Other filenames, missing tables, malformed JSON and database integrity failures cause it to stop. The app's private state format can change between releases.

## Missing chat links

Existing links and memberships are preserved. New local links use SQLite assignments first, then surviving global-state snapshots. Snapshot project IDs are matched through a same-host mapping or an unambiguous root match. Explicit projectless choices are respected. All project roots are retained.

Optional inference uses an exact recorded working-directory/project-root match:

```powershell
python restore_projects.py --infer-from-cwd --details
python restore_projects.py --infer-from-cwd --apply
```

Inference is a guess about historical membership, even when the path matches. Shared roots are skipped; subdirectories are not matched. Root matching from an old snapshot can also be misleading if a project was deleted and recreated at the same path. Review locally before applying.

The helper leaves SQLite, migration flags, pending migration IDs, app-server ID registries, remote memberships and unrelated settings unchanged. Recovered JSON links may still require the app to migrate them; this helper does not mark that migration complete.

## Undo and privacy

Before writing, the helper makes a uniquely named `.codex-global-state.json.backup-*` in the Codex home and writes the replacement atomically. It checks for running Codex processes and changed state again before replacing the file. These checks cannot prevent you from launching the app during the final write, so keep it closed.

To undo, fully quit Codex and copy the backup over `.codex-global-state.json` in the same directory. This restores the entire JSON file to its pre-recovery contents; any subsequent JSON settings changes will also be reverted. The helper does not edit `.codex-global-state.json.bak`.

If a crash leaves `.codex-sidebar-recovery.lock`, verify no recovery process is running before removing that lock. Do not remove the global-state file.

The script queries project names, roots, chat IDs, working directories and project IDs; it does not query conversation bodies from SQLite. It loads complete global-state JSON locally to preserve unrelated fields; that JSON may also contain private information. Its normal output contains only counts and a generic backup filename. Global-state files and backups can contain private information. **Never upload databases, state JSON, backups, configuration, authentication files or `--details` output to this repository.** The included `.gitignore` excludes common local data files.

Validation covers synthetic data and filesystem failure cases. Compatibility with the live Desktop UI across releases is not established. Run the synthetic checks with `python -m unittest discover -s tests -v`.

## License

MIT. See [LICENSE](LICENSE).

## Attribution

Created with assistance from GPT-6.1 Sol. This is an independent community project, not affiliated with, endorsed by, or supported by OpenAI.


