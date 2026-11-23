# Switchboard Knowledge — SilverBullet 3 add-on

Standalone [Switchboard](https://github.com/onogai/agent-switchboard) add-on built on the official **SilverBullet 3.0 edge-slim** image. It provides a private, human-readable Markdown knowledge space and a bearer-authenticated MCP endpoint.

Existing add-on ID **silverbullet** and instance names are intentionally unchanged. Upgrading an installed SilverBullet 3 add-on does **not** migrate, rename or rewrite the Markdown files.

## Components and storage

Three private Compose services, with no published host ports:

- **silverbulletapp:** official image pinned by digest, with upstream MCP and shell disabled.
- **silverbulletbootstrap:** idempotently provisions SilverBullet 3 and its search model using the authenticated admin API, then exits.
- **silverbulletadapter:** FastMCP server with a dedicated bearer token, a non-admin SilverBullet API token, and read-only local Markdown visibility for browsing.

The installer owns persistent state at `server/` (accounts, model, indexes), `space/` (Markdown notes and attachments), and private `secrets/`. Containers have read-only roots, no Linux capabilities, and only private-network access. The agent's upstream account is restricted to write access to the private Knowledge space, **without Runtime API**. The adapter never proxies arbitrary SilverBullet shell, runtime or admin methods.

## Automatic semantic search

On the first install **and on subsequent bootstrap runs**, the bootstrap selects a search model via the supported SilverBullet Admin API. Default:

`KNOWLEDGE_SEMANTIC_MODEL=granite-embedding-97m-multilingual-r2`

The other supported values are `multilingual-e5-small` or `off`. This environment variable is passed through to the bootstrap Compose service. Existing matching configuration is left untouched: **no repeated download and no index rebuild**. Changing the model deliberately rebuilds embeddings; existing Markdown content is unaffected.

SilverBullet downloads the chosen model and indexes pages asynchronously. Check `notes_search_status` before expecting `mode="natural"` and `notes_related_pages` to give semantic results. While the index is building, `natural` may fall back to lexical results with a `partial` flag/notice. Once idle, SilverBullet unloads the model from RAM after five minutes; indexes remain on disk. No external embedding API is used.

## MCP tool catalog (16 tools)

| Capability | Tools |
| --- | --- |
| Browse | `notes_overview`, `notes_list_pages`, `notes_read_page`, `notes_backlinks` |
| Search | `notes_search`, `notes_search_ranked`, `notes_search_status`, `notes_related_pages` |
| Create | `notes_capture`, `notes_create_wiki_page` |
| Edit | `notes_update_page`, `notes_edit_page` |
| Organize | `notes_move_page`, `notes_delete_page` |
| Feedback | `notes_list_agent_feedback`, `notes_resolve_agent_feedback` |

**Important contracts:**

- `notes_search` remains the legacy local keyword scan. Prefer `notes_search_ranked` for indexed `words`, `phrase`, or `natural` searches.
- `notes_list_pages` returns bounded paths with prefix/offset pagination, not entire documents; `notes_backlinks` finds **explicit** `[[wiki links]]`, not inferred semantic references.
- Agent writes are confined to `Inbox/` and `Wiki/`. Content writes reject executable SilverBullet Markdown.
- Existing-page operations require the **ETag from `notes_read_page`**. `notes_edit_page` replaces exactly one occurrence, `notes_update_page` replaces the whole document, and `notes_delete_page` additionally requires `confirm=true`.
- `notes_move_page` copies to a new path, then deletes the source under its ETag. It attempts to remove the new copy if the source revision changes. This is not an atomic server-side rename, and **links pointing to the old page are not rewritten**.
- Transient SilverBullet `SearchError::Busy` HTTP 429 is retried at most twice for search/related reads (respecting a bounded `Retry-After`). Persistent busy errors are reported as busy, not as account quotas.

## Install and update

For an empty new instance, using a Switchboard version with schema-v2 add-on support:

```sh
./switchboard addon https://github.com/onogai/agent-switchboard-silverbullet.git --name silverbullet3 --target production
./switchboard addons
```

For an **existing** SilverBullet 3 instance, use the normal Switchboard add-on update procedure on the same instance. Keep its existing `server/`, `space/` and `secrets/` directories. **Do not run the SilverBullet 2 → 3 migration again**. The bootstrap recognizes the configured accounts, space, token and model, and applies only necessary changes. New tools may require enabling their grants in Switchboard Manager.

For a genuinely old 2.12 installation, see [docs/silverbullet-3-migration.md](docs/silverbullet-3-migration.md); that migration is a separate, one-time task.

## Verification

The default GitHub Actions workflow runs compile and unit tests once per push:

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_*.py'
```

An optional disposable Docker acceptance test exercises the actual official server and private MCP transport without touching production. It explicitly turns **semantic search off** during that smoke test to avoid downloading a model solely for infrastructure verification; bootstrap model selection is covered by dedicated unit tests.

```sh
python -m scripts.silverbullet_full_addon_compose_e2e --confirm-disposable-test
```

Never paste generated admin passwords or bearer tokens into chats or issue reports. An administrator can inspect the password from the private add-on secrets directory on the Docker host.
