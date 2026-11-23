# SilverBullet 3 edge: parallel migration runbook (not yet deployed)

Status: **development branch only**. This branch does not automatically upgrade or migrate production.

## Breaking changes to accommodate

* Edge 3.0 removes legacy **single-space mode**. The existing 2.12.1 configuration using `--single`, `SB_USER` and `SB_AUTH_TOKEN` cannot simply be carried over.
* A new 3.0 instance requires Dashboard initialization, an account, a space and a **per-account HTTP API token**. Its native `/.mcp` requires OAuth; our private Switchboard adapter can continue using the HTTP API with an account token.
* Executable `space-lua` and `space-style` move to `Library/`, including `CONFIG.md` to `Library/Config.md`. Do not blindly execute older scripts.
* The search index is stored in server data and is rebuilt; do **not** copy a 2.x browser cache/index. Semantic indexing is disabled by default and needs a Dashboard setting and a model download.
* The full text and semantic search HTTP API (`/.help`, `/.search/*`) does **not** require Chromium.

## Safe migration stages

1. Inventory the running old instance: image **digest** (not merely tag), Compose configuration, source-space mount, number and byte size of Markdown and attachments, current tool grants. Confirm old data isn't already in the add-on's target space.
2. Stop old writers for the final snapshot. Produce an archive of the **entire** old space (including hidden files and attachments) outside both containers. Record a SHA-256 manifest. Do not back up plaintext service tokens together with public artifacts.
3. Start a fresh **parallel** 3.0 edge instance on an isolated, private Docker network, with a new data directory and an immutable tested image digest. Never mount production data in the test instance.
4. Initialize its Dashboard, one account and a dedicated space, then create an account-scoped API token. Use dedicated token mounts with mode 0600. Restrict access to the private Switchboard network. Do not expose Dashboard publicly.
5. Copy the snapshot into **the newly created space folder** (not the Dashboard's top-level server-data directory), preserving paths and attachments. Compare file names, counts, sizes and SHA-256 hashes before letting any migration wizard rewrite files.
6. Run the version's migration procedure and inspect `Library/Config`, executable Lua/styles, links and images. Preserve both before/after snapshots. Rebuild the search index; check `GET /.help`, `GET /.search/status`, ranked search, ETag read/write, write access restrictions and authentication rejection.
7. Run an isolated MCP acceptance test for the full tool catalog. Compare answers to known knowledge queries. Optional semantic search is enabled only after measuring resource usage and quality.
8. Only after verification, reconfigure Switchboard grants to the new MCP source and confirm the read/write and user-facing UI behavior. Retain the old instance and its immutable backup for rollback.

## Rollback

Disable new MCP source, enable the previous one, and restore the old read/write path without applying any 3.0-specific changes to the 2.12.1 space. Never mount a 3.0-migrated data folder directly into 2.12.1.

## Exit criteria

- [ ] 3.0 image digest pinned and tested
- [ ] Dashboard initialized and token security validated
- [ ] Source inventory / backup verified
- [ ] Target inventory matches source, except documented migration transformations
- [ ] Search and all MCP tools pass live tests
- [ ] User UI can view and edit expected pages
- [ ] Switchboard cutover verified
- [ ] Rollback procedure validated
