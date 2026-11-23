# SilverBullet 3: parallel migration and cutover (not yet executed)

The code in the add-on is updated for the pinned SilverBullet 3.0 **edge-slim** build, but **the actual Docker install and production knowledge migration are not yet tested**.

## Why a parallel instance

SilverBullet 3 removes legacy `--single` mode and moves server metadata into an account-managed Dashboard. The old 2.12.1 folder cannot safely be mounted as the new 3.0 `/data` root.

The new add-on therefore has:
- `state/server` → `/data`: a fresh **empty** server root, later containing `users.json`, `spaces.json`, `server.json` and search indexes.
- `state/space` → `/knowledge`: a separate Markdown space, even when it already contains notes.
- `state/secrets`: locally generated credentials, with the new account API token minted by SilverBullet itself.

**Never put the old Markdown pages directly into `state/server`.** An unconfigured SilverBullet 3 server detects a non-empty `/data` as a legacy installation and opens the migration wizard.

## Automatic first-run setup

The private one-shot `silverbulletbootstrap` service:
1. Calls `/.setup/api/complete` with a generated admin password and a `Knowledge` space bound at `/` whose content folder is `/knowledge`. No starter-kit files are added.
2. Logs in to the Dashboard, creates a non-admin `switchboard-agent` account.
3. Grants the agent `write` to the specific `Knowledge` space with `runtimeApi: false`.
4. Mints an account API token, stores it atomically as `state/secrets/token`, checks it without admin cookies and exits.
5. Only then does the MCP adapter start.

These APIs are reachable only on the private add-on network. Do not connect untrusted containers to that network. `SB_MCP=0`, `SB_SHELL_BACKEND=off`; no host ports are published. A UI reverse proxy, if desired, must be configured separately.

The initialization is idempotent on repeat runs. If the old admin password no longer matches, the API token is invalid, or access settings differ unexpectedly, it **fails closed**; it does not reset credentials or overwrite other users.

## Installation test (next phase; disposable resources only)

On the operator's Docker host and a checkout of this migration branch:

```sh
python -m unittest discover -s tests -v
python -m scripts.silverbullet_full_addon_compose_e2e --confirm-disposable-test
```

The Docker E2E must check three services, completion of bootstrap, `server/` metadata, agent API token, the eleven MCP tools, auth rejection, live SilverBullet `/.help` and full-text search, CRUD/ETags, and disposable data persistence. The test creates a random temporary project/network and never mounts production knowledge.

Do not merge into the installable main release until this acceptance completes.

## Production data migration (after clean install passes)

1. Inventory the old 2.12.1 instance: image, source mount, Markdown files and attachments, link paths, counts, bytes and SHA-256 manifest. Confirm no original production directory is shared with the new add-on.
2. Stop the old writers for a consistent snapshot. Archive *all* pages and attachments, including hidden files, to independent persistent storage outside both containers. Keep authentication secrets separately.
3. Install the **new instance name**, e.g. `silverbullet3`, using the normal Switchboard CLI. Its bootstrap creates the space and private credentials without user intervention.
4. Copy the backup's content into `addons/<instance>/state/space/`, **not** `state/server/`. No automatic copying is performed by the installer.
5. Verify paths, content hashes, counts, attachments and wiki links. SilverBullet 3 requires executable Space Lua/Space Style under `Library/` and migrates `CONFIG.md` to `Library/Config.md`; record any intended transformations.
6. Verify `/.help`, `/.search/status`, keyword search, selected notes and write/ETags through the non-admin token. Optional semantic search stays off until separately benchmarked.
7. Verify the UI through a trusted operator-only route and then approve/grant the new MCP source in Switchboard.
8. Switch consumers to the new MCP only after verified queries and a complete old-space backup. Do not drop the old add-on yet.

## Rollback

Disable the new MCP source and re-enable the previous 2.12.1 source and grants. The old files must remain byte-for-byte untouched. Never mount 3.0-managed `server/` or a post-migration space directly in the older 2.12.1 instance.

## Acceptance gates

- [ ] Pinned 3.0 image digest pulls on the target architecture
- [ ] Disposable full Compose test passes
- [ ] Admin/agent accounts, access and tokens verified
- [ ] Old knowledge backup verified
- [ ] Imported Markdown and attachments verified
- [ ] UI and 11 MCP tools verified
- [ ] Switchboard grants and cutover verified
- [ ] Rollback independently validated
