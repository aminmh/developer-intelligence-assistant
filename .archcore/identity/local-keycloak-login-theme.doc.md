---
title: "Local Keycloak Login Theme"
status: draft
tags:
  - "auth"
  - "frontend"
---

## Overview

The embedded Keycloak service uses the `developer-assistant` login theme to align its sign-in page with the app’s dark navy and indigo palette.

## Content

- Theme files live under `web/keycloak/themes/developer-assistant/login` and extend Keycloak’s built-in login theme. In `@web/keycloak/themes/developer-assistant/login/resources/css/styles.css`, the password visibility toggle sits inside the field and animates its icon while respecting reduced-motion preferences.
- `web/docker-compose.yml` mounts the theme directory read-only at `/opt/keycloak/themes`.
- `web/keycloak/developer-assistant-realm.json` selects the theme for newly imported realms.
- Existing realm settings persist in the `keycloak-data` volume; select the theme in Realm Settings → Themes → Login theme when applying to an existing realm.
- The `up-lite` deployment uses an external OIDC provider, whose administrator controls its login page.

## Examples

- Start the embedded provider with `make up` from `web/` or use the `local-auth` Compose profile.
- After modifying theme files, restart or recreate the Keycloak container to reload them.