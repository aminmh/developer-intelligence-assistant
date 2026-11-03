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
Existing layouts are restored as saved and are not automatically regenerated
when the schema changes. The original analysis remains unchanged.

Use **Rebuild from saved schema** if the diagram is out of date. Rebuilding
replaces the saved layout, including positions and annotations, so the app asks
for confirmation. Save structured edits first.

In Draw.io, select **Extras → Preview migration** to save the current diagram
and review and download a candidate Laravel or Symfony/Doctrine migration for
MySQL or PostgreSQL. Choose the target and migration timestamp, then review
the displayed changes and generated PHP before downloading. The draft panel
also offers migration preview. Drops require explicit confirmation. Generation
supports a limited set of table and column additions and removals, plus some
isolated named key, index, and relationship additions or removals. Isolated
column renames are supported on PostgreSQL and MySQL 8.0 or newer; PostgreSQL
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
definition against the database. Character sets, collations, expressions and
other unsupported attributes remain blocked. `default_kind` is `none`, `null`,
or `literal`; a literal uses the column's `default` value. PostgreSQL supports
numeric and boolean defaults and dropping a default; string or expression
defaults, auto-increment changes and unsupported combinations remain blocked. Review the
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
