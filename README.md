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

To start the development container directly, run:

```sh
OIDC_LITE_INTERNAL_ISSUER=http://keycloak:8080/realms/developer-assistant \
  docker compose --profile dev --profile local-auth up -d keycloak web-dev
```

For host-run `npm run dev`, use `docker compose --profile local-auth up -d keycloak`;
`.env` already points server requests to localhost. Sign in as
`developer-admin`; the generated
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
