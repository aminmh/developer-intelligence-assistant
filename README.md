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

Start the production web container with `docker compose up -d`, or the local
development container with `docker compose --profile dev up -d keycloak web-dev`.
For host-run `npm run dev`, use `docker compose up -d keycloak`; `.env` already
points server requests to localhost, and Compose overrides that address for
containerized web servers. Sign in as `developer-admin`; the generated
password is stored as `KEYCLOAK_BOOTSTRAP_PASSWORD` in `web/.env`. The separate
Keycloak console account is `admin`, with its password in
`KEYCLOAK_ADMIN_PASSWORD`.
