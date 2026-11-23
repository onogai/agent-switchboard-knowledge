# Switchboard SilverBullet 3.0 Add-on

Standalone, self-contained [Switchboard](https://github.com/onogai/agent-switchboard) add-on bundling **SilverBullet 3.0 edge slim**, automated private-space setup, and an authenticated MCP adapter.

> Migration branch status: **unit-checked, but real Docker installation / production cutover NOT yet tested**. The 2.12.1 installation must remain untouched until a clean disposable E2E succeeds.

## Architecture

Three private, host-port-free Docker Compose services:

1. **`silverbulletapp`** runs the official 3.0 `edge-slim` multi-arch manifest **pinned by digest** (no Chromium or Runtime API).
2. **`silverbulletbootstrap`** runs only during setup and exits successfully after provisioning.
3. **`silverbulletadapter`** provides private bearer-authenticated FastMCP, preserving the original seven note/feedback tools and adding four SilverBullet 3 discovery/search tools.

Persistent state is kept in two **independent** directories managed by Switchboard:

| Path relative to add-on state | Container path | Contents |
| --- | --- | --- |
| `server/` | `/data` | SilverBullet 3 accounts, spaces, search indexes and server metadata |
| `space/` | `/knowledge` | Markdown pages and attachments |
| `secrets/` | private mounts | Generated admin password, agent password, MCP bearer and SilverBullet account token |

The `/data` directory is empty on first boot. It **must not** contain the initial Markdown space: SilverBullet 3 would otherwise mistake it for a legacy single-space installation and start the migration wizard.

## Automatic bootstrap

On first installation, the bootstrap service uses the private SilverBullet setup API to:

- Create the `admin` account with a generated password, and an empty `Knowledge` space bound at `/`, backed by `/knowledge`.
- Keep anonymous access at `none`.
- Create a separate **non-admin** `switchboard-agent` account with `write` access only to `Knowledge`, and `runtimeApi: false`.
- Issue a scoped account API token and atomically store it as `secrets/token`.
- Verify the token using a **fresh HTTP client without admin cookies**.
- Exit successfully so Docker Compose may start the adapter.

The bootstrap service is idempotent: repeated runs verify the existing state and token without creating more accounts, spaces, or tokens. Unexpected access rights, changed passwords or invalid existing tokens **fail closed**, rather than silently resetting credentials.

No public host ports are bound. Upstream `SB_MCP=0`, `SB_SHELL_BACKEND=off` and `SB_DISABLE_SERVICE_WORKER=1`; all three containers drop capabilities, use read-only roots and mount only add-on state.

## MCP tools

The original seven remain available:

- `notes_search`, `notes_read_page`, `notes_capture`, `notes_create_wiki_page`, `notes_update_page`, `notes_list_agent_feedback`, `notes_resolve_agent_feedback`.

Four SilverBullet 3 HTTP API tools were added:

- `notes_overview`: `GET /.help`, discovers folders, top pages, tags and agent skills.
- `notes_search_status`: `GET /.search/status`, inspects search index readiness and enabled modes.
- `notes_search_ranked`: `POST /.search/query`, `words`, `phrase` or optionally `natural`.
- `notes_related_pages`: `POST /.search/related`, requires configured semantic indexing.

The original keyword scanner remains for compatibility, but the new ranked search uses SilverBullet's native index. **Semantic embeddings are disabled by default**; enabling them later requires a resource and retrieval-quality test. No additional vector database or Chromium is required for the 3.0 search API.

## Verification before deployment

For offline tests (requires Python 3.12+):

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_*.py' -v
```

**Installation acceptance is a separate next step**, with a disposable private network, temporary credentials and storage:

```sh
python -m scripts.silverbullet_full_addon_compose_e2e --confirm-disposable-test
```

The acceptance test must run on a host with Docker Compose and should verify all three services, bootstrap, 11 MCP tools, data isolation, writes, search and token rejection. It creates and removes only its randomly named test resources, not the production Markdown folder.

## Installing a *new* parallel instance (only after acceptance)

```sh
./switchboard addon https://github.com/onogai/agent-switchboard-silverbullet.git --name silverbullet3 --target production
./switchboard addons
./switchboard addon logs silverbullet3
```

The manager must support schema-v2 multi-service add-ons and `--target production`. This deploys an empty 3.0 instance; **the installer does not migrate existing notes or switch tool grants automatically**.

To sign in to the SilverBullet Dashboard, obtain the generated admin password **locally on the Docker host**, from the add-on's private `state/secrets/password` file. Never paste this value into a public issue, PR, log, chat or command-line argument.

## Knowledge migration and rollback

See [docs/silverbullet-3-migration.md](docs/silverbullet-3-migration.md). In summary: snapshot every source file and attachment, stop old writers for a consistent final copy, copy into the new `space/` directory (not `server/`), verify paths and SHA-256, rebuild/search, verify UI and MCP, then switch grants. Keep the old 2.12.1 container and backup for rollback.

Production storage is never mounted by unit tests or the disposable E2E test. The main branch's existing 2.12.1 deployment remains independently restorable until an explicit cutover.

## Notes

- The official image digest comes from GHCR's published 3.0 edge-slim package version and is pinned to prevent moving-edge upgrades.
- The 3.0 setup and Dashboard REST APIs are authenticated/private except for the first-run wizard; the wizard is reachable **only on Switchboard's private add-on network**, not through a host port. Avoid connecting untrusted containers to this network.
- UI access through a trusted Tailscale/reverse-proxy route is a separate operator task; this add-on intentionally never publishes the SilverBullet port.
