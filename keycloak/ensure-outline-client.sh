#!/bin/sh
set -eu

: "${KC_BOOTSTRAP_ADMIN_USERNAME:?Keycloak bootstrap username is missing}"
: "${KC_BOOTSTRAP_ADMIN_PASSWORD:?Keycloak bootstrap password is missing}"
: "${OUTLINE_OIDC_CLIENT_SECRET:?Outline OIDC secret is missing; run just outline-up}"

realm=developer-assistant
client_id=developer-assistant-outline
callback=http://localhost:3020/auth/oidc.callback
origin=http://localhost:3020
umask 077
config_file="$(mktemp)"
trap 'rm -f "$config_file"' EXIT HUP INT TERM

# The generated secret is hex, so it is safe to interpolate in this JSON.
printf '{"clientId":"%s","name":"Developer Assistant Outline","enabled":true,"protocol":"openid-connect","publicClient":false,"secret":"%s","standardFlowEnabled":true,"directAccessGrantsEnabled":false,"redirectUris":["%s"],"webOrigins":["%s"],"attributes":{"post.logout.redirect.uris":"%s/*"}}\n' \
  "$client_id" "$OUTLINE_OIDC_CLIENT_SECRET" "$callback" "$origin" "$origin" \
  > "$config_file"

/opt/keycloak/bin/kcadm.sh config credentials \
  --server http://localhost:8080 \
  --realm master \
  --user "$KC_BOOTSTRAP_ADMIN_USERNAME" \
  --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null

clients="$(/opt/keycloak/bin/kcadm.sh get clients -r "$realm" -q "clientId=$client_id")"
client_uuid="$(printf '%s' "$clients" | tr -d '\n' | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"

if [ -n "$client_uuid" ]; then
  update_file="$(mktemp)"
  trap 'rm -f "$config_file" "$update_file"' EXIT HUP INT TERM
  sed "s/^{/{\"id\":\"$client_uuid\",/" "$config_file" > "$update_file"
  /opt/keycloak/bin/kcadm.sh update "clients/$client_uuid" -r "$realm" -f "$update_file" >/dev/null
else
  /opt/keycloak/bin/kcadm.sh create clients -r "$realm" -f "$config_file" >/dev/null
fi

# Older local realms seeded this user with developer-admin@localhost, which
# Outline rejects as an invalid OIDC email. Repair only that known placeholder.
seed_user="$(/opt/keycloak/bin/kcadm.sh get users -r "$realm" -q username=developer-admin)"
seed_user_id="$(printf '%s' "$seed_user" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
seed_user_email="$(printf '%s' "$seed_user" | sed -n 's/.*"email"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
if [ -n "$seed_user_id" ] && [ "$seed_user_email" = "developer-admin@localhost" ]; then
  /opt/keycloak/bin/kcadm.sh update "users/$seed_user_id" -r "$realm" \
    -s email=developer-admin@developer-assistant.test \
    -s emailVerified=true >/dev/null
fi

printf '%s\n' 'Outline OIDC client is ready.'
