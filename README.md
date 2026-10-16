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

The Definitions view's expanded rows can show method callers, callees, and hot-path share when
Phase 4 runtime telemetry is available. Counts come from selected PHP method hooks; hot-path share
is a statistical estimate based on retained Excimer samples. Missing profiles or hooks leave those
details empty. See the workspace's `api/docs/telemetry/runtime-phase4.md` for PHP and API setup.

Runtime request details also include a trace summary with de-overlapped MySQL/Redis span estimates
and retained Excimer hotspots when available. Remaining request time can include uninstrumented
work; hotspot percentages are sample estimates. See the workspace's
`api/docs/telemetry/runtime-phase5-trace-summary.md`.
