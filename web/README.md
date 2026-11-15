<div align="center">
<img width="1200" height="475" alt="GHBanner" src="https://ai.google.dev/static/site-assets/images/share-ais-513315318.png" />
</div>

# Run and deploy your AI Studio app

This contains everything you need to run your app locally.

View your app in AI Studio: https://ai.studio/apps/06006a5e-ca6a-4122-888d-844832fb7844

## Run Locally

**Prerequisites:**  Node.js


1. Install dependencies:
   `npm install`
2. Set the `GEMINI_API_KEY` in [.env.local](.env.local) to your Gemini API key
3. Run the app:
   `npm run dev`

## Development authentication

`npm run dev` and the `web-dev` Compose service disable sign-in and use a
local administrator identity. The development server is intended for trusted
local use only: host-run development listens on loopback, and Compose publishes
the development port on loopback. Anyone able to reach it has administrator
access. Production and other `NODE_ENV` values continue to require OIDC.

To start the development container, run:

```sh
docker compose --profile dev up -d web-dev
```

Recreate an existing development container after changing the port binding.

## Local sign-in with Keycloak

The Compose stack starts a local Keycloak server on `http://localhost:8180` and
imports the `developer-assistant` realm, the `developer-assistant-web` OIDC
client, and the initial administrator account. The app's browser issuer is
`http://localhost:8180/realms/developer-assistant`; its container uses
`http://keycloak:8080/realms/developer-assistant` for server-to-server OIDC
requests. Keep the client secret and passwords in the ignored `web/.env` file.
The local realm uses the `developer-assistant` login theme in
`keycloak/themes`; it styles Keycloak's built-in login flow with the app's
dark navy and indigo palette. Restart Keycloak after changing theme files.
Existing realms retain their stored settings after import, so select
`developer-assistant` under **Realm Settings → Themes → Login theme** once when
upgrading an existing Keycloak data volume. External OIDC deployments own
their login page and are not changed by this theme.

Start production Web with `make up`; it starts Keycloak using the `local-auth`
profile. To start both containers directly, run:

```sh
OIDC_LITE_INTERNAL_ISSUER=http://keycloak:8080/realms/developer-assistant \
  docker compose --profile local-auth up -d keycloak web
```

For local production-mode sign-in, sign in as `developer-admin`; the generated
password is stored as `KEYCLOAK_BOOTSTRAP_PASSWORD` in `web/.env`. The separate
Keycloak console account is `admin`, with its password in
`KEYCLOAK_ADMIN_PASSWORD`.

For a lower-memory production launch with an external OIDC provider, configure
`OIDC_ISSUER` and `OIDC_LITE_INTERNAL_ISSUER` if the Web container needs a
different provider URL, then run `make up-lite` (or `just up-lite` from the
workspace root). This mode does not start Keycloak.

## Optional local Outline knowledge base

Outline can run locally for authoring business rules used by Code Quality. It
uses the existing local Keycloak realm for sign-in, with PostgreSQL and Redis
in separate Compose containers. It is excluded from `just up-dev` and the
regular Web startup commands.

Start it explicitly from the workspace root:

```sh
just outline-up
```

Then open `http://localhost:3020` and sign in as `developer-admin` with the
password stored in `KEYCLOAK_BOOTSTRAP_PASSWORD`. If `web/.env` does not exist,
copy `.env.example` to `.env` and configure the existing Keycloak settings
first. The command generates missing Outline and database secrets in the
ignored `web/.env` file, provisions the Outline OIDC client in the existing
realm, and starts the Outline profile. The Outline port binds to loopback. Its
PostgreSQL database and uploaded files persist in named Docker volumes.

Use `just outline-status` and `just outline-logs` to inspect the service. Stop
and remove only the Outline, PostgreSQL, and Redis containers with
`just outline-down`; this preserves database and file volumes and leaves the
shared Keycloak service running. Starting Outline does not require putting an
Outline container into the normal development profile.

In Code Quality, select a project and open **Business rules**. Create a dedicated
Outline collection for that project, generate a read-only API token for an
Outline user who can read those documents, load the collection list, and save
the selected collection. For the local profile, use http://outline:3000 as the
instance URL; this service name resolves on the application Docker network.
Tokens are stored in the host macOS Keychain or Linux Secret Service. Code
Quality synchronizes documents before each assessment and never writes to
Outline. Existing locally stored rules can be exported from that tab as
Markdown for manual migration.

Runtime request details also include a trace summary with de-overlapped MySQL/Redis span estimates
and retained Excimer hotspots when available. Remaining request time can include uninstrumented
work; hotspot percentages are sample estimates. See the workspace's
`api/docs/telemetry/runtime-phase5-trace-summary.md`.

### Draw.io schema layout editor (experimental)

The optional local editor runs on a separate browser origin. Start it with
`docker compose --profile drawio up -d drawio`, and start the development web
service as usual. The development web container defaults to
`http://localhost:18081`; override `VITE_DRAWIO_ORIGIN` before starting it when
needed. Production builds leave the editor disabled unless
`VITE_DRAWIO_ORIGIN` is set at build time to a dedicated HTTPS origin. Rebuild
the web image after changing that value. The bundled image digest is a candidate
for local testing, not an approved production deployment.
Recreate an existing editor container to mount the bundled schema plugin:
`docker compose --profile drawio up -d --force-recreate drawio`.

To load a running database, select its project and environment in **Runtime →
Containers → Load database schema**, then choose **Find containers** in Database
Schema. This administrator workflow requires the desktop Runtime setup helper.
Only containers already reporting for that project are eligible. `just php-otel-setup`
labels the project's MySQL, MariaDB and PostgreSQL Compose services for reporting; rerun
it to add a database to a project that is already set up. Reporting recreates the
database container once. Discovery verifies PostgreSQL, MySQL or MariaDB
from the inspected container, and defaults to its configured database and PostgreSQL
`public` schema when available.

For a remote reporting host, use the loader's SSH association controls, inspect and
confirm its fingerprint, and use an account with existing Docker access. The helper
reuses Runtime's SSH agent and OS-stored credentials; schema host associations are
separate from ClickHouse storage settings. Directly reachable SSH hosts are supported.
The container needs an installed `psql`, `mysql` or `mariadb` client and usable
configured credentials, including supported password secret files. Credentials stay
on the execution host, and the configured account is never silently replaced.

Select the database and applicable schemas, then click **Load schema in Draw.io**.
This explicit action reads catalog metadata in a read-only transaction, saves a new
baseline and editable draft, and opens that draft. Refreshing Runtime or selecting
a project never extracts a schema. Imports fail after 60 seconds or above 4 MiB;
select fewer schemas and retry. Unsupported definitions remain visible as inspection
metadata with coverage warnings and block migrations that cannot preserve them.
Failed extraction preserves the current draft. A blocked popup or editor outage
leaves the new draft saved; choose **Open saved draft in Draw.io** to retry.

The **Open Draw.io** shortcuts in Runtime and Database Schema open the configured
service independently without creating a draft. Missing configuration displays setup
guidance for `VITE_DRAWIO_ORIGIN`.

Create or open a schema draft, save any structured schema edits, and select
**Open Draw.io editor**. Use **Extras → Add schema table** or **Add schema
column**, edit table and column labels, or delete unreferenced tables and
columns. Select a column and use **Extras → Edit column properties** to edit
nullability, defaults and auto-increment. Use **Edit table keys and indexes**
and **Add or edit relationship** in the same menu for constraints and
relationships. These property editors use JSON and stable IDs. Other shapes are
annotations. Saving in Draw.io updates the draft in the application.
Select a table and choose **Extras → Show table runtime usage** to see its
observed operation count for the runtime project selected in the application.
Use **Extras → Find schema table or column** to select and reveal an entity by
name in the editor.
With the editor connected, select an original table or relationship in the
application and choose **Show selection in Draw.io** to reveal the matching
stable ID. In Draw.io, select a table, column, or relationship and use
**Extras → Show original schema details** to select it in the application.
The application details describe the original schema, not unsaved edits; newly
added entities have no original details.
Existing layouts are restored as saved and are not automatically regenerated
when the schema changes. The original analysis remains unchanged.

Use **Extras → Export schema JSON** in Draw.io to download the current diagram's
structured schema, including changes not yet saved to the draft. Export does not
save the draft; use Draw.io's save action separately. Visual annotations are not
included in the schema JSON.
**Extras → Export schema DBML** exports the current diagram for interchange;
DBML does not preserve stable IDs, all column metadata, or the distinction
between unique keys and indexes. Use JSON when those details must survive
re-import. DBML export rejects identifiers and types it cannot represent safely.

Structured schema JSON imports retain named and composite keys, indexes,
relationships, referential actions, and their IDs in the draft. The schema
preview shows only the first column of a composite relationship; all its
columns remain in the Draw.io draft and migration comparison. The server
validates the complete imported structure before creating a draft.
DBML imports also retain named column indexes, composite primary keys, and
relationship names, columns, and actions. Index expressions, explicit index
types, unnamed non-primary indexes, and many-to-many relationships require
changes to the DBML before creating a draft.
SQL imports retain supported named primary and unique constraints, named
column indexes, and composite foreign keys with supported delete and update
actions. Expression, partial, and ordered indexes, checks, and unsupported
foreign-key attributes block draft creation. SQL import remains a limited
parser; verify imported definitions before generating a migration.

Optional database inspection reads column metadata without changing the draft or
its original baseline. Configure `SCHEMA_INSPECTION_TARGETS` on the Python
service as a JSON object mapping target names to server-side connection
settings. For example (supply the password through your deployment secret
manager and use a database account restricted to metadata reads):

```json
{"reporting": {"engine": "postgresql", "host": "db.example.org", "port": 5432,
 "database": "app", "schema": "public", "username": "schema_reader",
 "password": "<secret>", "ca_file": "/run/secrets/database-ca.pem"}}
```

For MySQL, set `engine` to `mysql` and the appropriate port and database. The
service requires verified TLS and executes only fixed metadata queries inside a
read-only transaction. Restart the service after changing its environment.
From Draw.io, choose **Extras → Inspect database metadata**, enter the configured
target name, and consent to the connection. The report includes observed
columns and a name-only comparison with the original schema. It is limited to
5,000 columns and does not inspect keys, indexes, relationships, or other
objects. It cannot certify the baseline or make migration generation safe by
itself. Target configuration must be controlled by the service administrator.

Use **Rebuild from saved schema** if the diagram is out of date. Rebuilding
replaces the saved layout, including positions and annotations, so the app asks
for confirmation. Save structured edits first.

In Draw.io, select **Extras → Preview migration** to save the current diagram
and review and download a candidate Laravel or Symfony/Doctrine migration for
MySQL or PostgreSQL. Choose the target and migration timestamp, then review
the displayed changes and generated PHP before downloading. The draft panel
also offers migration preview. Drops require explicit confirmation. Generation
supports a limited set of table and column additions and removals, including
new unnamespaced tables with named primary and unique keys on supported
columns, followed by their named indexes. MySQL primary keys must be named
`PRIMARY`; auto-increment columns still require manual migration. Adding columns with
explicit collation or MySQL definition metadata is blocked until those
attributes can be preserved. It also supports some
isolated named key, index, and relationship additions or removals. Multiple
independent index additions and adding nullable columns with their named unique
keys and indexes are also supported; keys and indexes are created after their
new columns. Mixed index
additions and removals remain blocked. Unique indexes use index DDL for both
Laravel and Doctrine. Isolated column renames
are supported on PostgreSQL and MySQL 8.0 or newer; PostgreSQL
also supports changes to supported column types and nullability. For a change
to text, enter the verified target collation in the column properties in
Draw.io; changes from text to a non-text type remain blocked. For MySQL type,
nullability, default, or auto-increment changes, enter a `mysql_definition`
object in the column properties, for example:

```json
{"unsigned": false, "charset": null, "collation": null, "comment": null,
 "default_kind": "none", "other_attributes_absent": true}
```

Confirm `other_attributes_absent` only after checking the complete target
definition against the database. For a text column, provide both the character
set and collation when they are part of the definition; verify that the pair is
supported by the target database. When both the original and edited definitions
are known, a change to those attributes is included in the migration preview.
Adding metadata that was missing from the original baseline only confirms the
target definition; it does not establish what the original value was.
Auto-increment is also recorded as unknown when the source does not provide it.
New columns require an explicit setting, and MySQL column definition changes
require a confirmed target setting. An unknown baseline cannot establish whether
auto-increment changed, so such changes require manual migration review. Older
drafts that already recorded false cannot recover missing source metadata.
Expressions and other unsupported attributes
remain blocked. `default_kind` is `none`, `null`,
or `literal`; a literal uses the column's `default` value. PostgreSQL supports
numeric and boolean defaults and dropping a default; string or expression
defaults and auto-increment changes remain blocked. PostgreSQL can combine
independent nullability and default changes across columns; dependent combinations
remain blocked. Review the
baseline, migration ordering, and generated code against the target project and
database. The application does not write or run migrations in that project.
Preview asks the configured model to generate a migration from the structured
diff; validation accepts only a narrow set of supported PHP operations and can
reject otherwise valid alternatives. It does not independently validate
arbitrary PHP. The Python service must have PHP available to check syntax.

If the popup is blocked, allow popups and retry. If the editor does not connect,
check that the service is reachable at the configured origin and that browser
or proxy isolation headers preserve `window.opener`. Disconnect an abandoned
editor session before trying again. Unsaved received layouts and schema edits
are recoverable in the same browser tab when session storage is available; open
the draft again to restore them. A revision conflict requires reviewing the
newer draft before saving. Changes still open only in Draw.io have not yet been
received by the app and should be saved there first. Production deployment
requires independent review of the image, security headers, and data policy.
