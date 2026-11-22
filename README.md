# Switchboard SilverBullet Add-on

A standalone, self-contained [Switchboard](https://github.com/onogai/agent-switchboard) add-on bundling an official SilverBullet server and a private authenticated MCP adapter.

This repository is independent of the Switchboard Core codebase. It owns its own persistent Markdown space, credentials, container lifecycle, and acceptance tests. **No production data is mounted or migrated automatically.**

## Contents

- `silverbulletapp`: pinned official SilverBullet server (`2.12.1-slim`).
- `silverbulletadapter`: authenticated FastMCP server with seven note/search/feedback tools.
- `compose.addon.yaml` and `switchboard-addon.yaml`: schema-v2 installer integration.
- `scripts/`: read-only checks and a strictly isolated two-container Docker acceptance test.
- `tests/`: local unit and contract tests.

## Install

On a host with an initialized **portable** Switchboard, install from this repository using the host's `./switchboard addon` command. The installer provisions `addons/<instance>/state/space` and private credentials, launches both containers on the private add-on network and discovers new MCP tools in review. **Approve and grant access manually** after acceptance.

```bash
./switchboard addon https://github.com/onogai/agent-switchboard-silverbullet.git --name silverbullet
./switchboard addons
```

A private repository requires the Docker host to have Git credentials/SSH access; an HTTPS clone from a private repository may require prior non-interactive authentication.

## Checks

```bash
python -m unittest discover -s tests -v
# On a SEPARATE disposable Docker daemon ONLY (NOT the production host):
python -m scripts.silverbullet_full_addon_compose_e2e --confirm-isolated-docker-host
```

The destructive E2E refuses to run on a Docker daemon with pre-existing containers. It creates an internal network, disposable state and credentials, verifies the seven MCP tools and filesystem read/write behavior, then cleans up only its own resources.

## Data ownership and migration

The installer's persistent source of truth is `addons/<instance>/state/space`, mounted read/write into SilverBullet and read-only into the adapter. The adapter modifies pages **only through SilverBullet's authenticated filesystem HTTP API**. Credentials live in `addons/<instance>/state/secrets`, not in the Markdown space.

For migration from an existing SilverBullet, first prove a clean install with disposable data. Separately back up the old space, stop old writers, copy **all** necessary Markdown and attachments from the Docker host into the new `space/` preserving ownership, verify counts/checksums, restart or reindex SilverBullet if needed, then manually grant new tools in Switchboard. Do not copy old credentials or blindly overwrite pre-existing files.

The old Core knowledge tools should remain in place until the new deployment, backup/restore and user-facing UI access have been verified. This repository does not perform a cutover or an automatic grant migration.
