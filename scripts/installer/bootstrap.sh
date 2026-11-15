#!/usr/bin/env bash
# Bash 3.2 compatible. Persisted installer data is never sourced as shell code.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
JUST_VERSION=1.58.0
BIN_DIR="$ROOT/.install/bin"
CACHE_DIR="$ROOT/.install/downloads"

fail() { printf 'Installer: %s\n' "$*" >&2; exit 1; }

compatible_just() {
    local version major minor patch
    version=$("$1" --version 2>/dev/null) || return 1
    # Accept stable numeric releases, never malformed output or prereleases.
    [[ "$version" =~ ^just\ ([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || return 1
    major=${BASH_REMATCH[1]}
    minor=${BASH_REMATCH[2]}
    patch=${BASH_REMATCH[3]}
    [[ "$major" = 1 ]] && (( 10#$minor >= 40 )) && [[ -n "$patch" ]]
}

select_release() {
    local system machine
    system=$(uname -s)
    machine=$(uname -m)
    # Digests verified against the official release's asset metadata:
    # https://github.com/casey/just/releases/expanded_assets/1.58.0
    case "$system:$machine" in
        Darwin:arm64|Darwin:aarch64)
            TARGET=aarch64-apple-darwin
            SHA256=50ae3e996c974a0bf32ea7d10f495070df33f1b43e0616b2769e3d4821ed8f48 ;;
        Darwin:x86_64|Darwin:amd64)
            TARGET=x86_64-apple-darwin
            SHA256=9a09cfef66aaa79da58203970103a0684307716caaabd3e9844cacc4dc0f4023 ;;
        Linux:arm64|Linux:aarch64)
            TARGET=aarch64-unknown-linux-musl
            SHA256=748237128c4c40cbdabc65e841d05ceba13cc23a91eaba395495894c1d9764df ;;
        Linux:x86_64|Linux:amd64)
            TARGET=x86_64-unknown-linux-musl
            SHA256=4a5cc2f53e6f0f8c59092a6cc38291eb729d46a7dd95d3ae582008881b84931d ;;
        *) fail "Unsupported platform $system/$machine. Install Just >= 1.40.0 and rerun bash install.sh." ;;
    esac
}

verify_archive() {
    local actual
    if command -v sha256sum >/dev/null 2>&1; then
        actual=$(sha256sum "$1") || return 1
    elif command -v shasum >/dev/null 2>&1; then
        actual=$(shasum -a 256 "$1") || return 1
    else
        fail 'SHA-256 verification needs sha256sum or shasum.'
    fi
    actual=${actual%% *}
    [[ "$actual" = "$SHA256" ]]
}

download_archive() {
    local archive url attempt delay http_code curl_status
    archive=$1
    url="https://github.com/casey/just/releases/download/$JUST_VERSION/just-$JUST_VERSION-$TARGET.tar.gz"
    # A prior process may have finished downloading before it was interrupted.
    if [[ -f "$archive.part" ]] && verify_archive "$archive.part"; then
        mv "$archive.part" "$archive"
        return
    fi
    command -v curl >/dev/null 2>&1 || fail 'curl is required to download Just.'
    attempt=0
    delay=1
    while :; do
        if http_code=$(curl --fail --location --show-error --proto '=https' --tlsv1.2 \
            --connect-timeout 20 --max-time 300 --continue-at - \
            --write-out '%{http_code}' --output "$archive.part" "$url"); then
            # curl can treat 416 as success for a supposedly complete file.
            # Only the checksum can tell whether that existing file is valid.
            if [[ "$http_code" != 416 ]]; then
                break
            fi
            curl_status=22
        else
            curl_status=$?
        fi
        # An oversized/corrupt completed partial produces HTTP 416. A server
        # refusing Range produces curl 33. Preserve those unusable bytes, then
        # retry at offset zero; retaining that offset would fail every rerun.
        if [[ -f "$archive.part" ]] && { [[ "$curl_status" = 33 ]] ||
            [[ "$curl_status" = 22 && "$http_code" = 416 ]]; }; then
            if verify_archive "$archive.part"; then
                mv "$archive.part" "$archive"
                return
            fi
            mv "$archive.part" "$archive.rejected.$(date +%s).$$.retry$attempt"
            printf 'Installer: server rejected the resume offset; retained unusable partial and restarting download.\n' >&2
        fi
        if (( attempt >= 3 )); then
            fail "Just download failed after 3 retries. Partial download retained at $archive.part; rerun bash install.sh to resume."
        fi
        printf 'Installer: retrying Just download in %ss (%s/3).\n' "$delay" "$((attempt + 1))" >&2
        sleep "$delay"
        attempt=$((attempt + 1))
        delay=$((delay * 2))
    done
    if ! verify_archive "$archive.part"; then
        mv "$archive.part" "$archive.rejected.$(date +%s).$$"
        fail 'Downloaded Just archive failed SHA-256 verification. Rejected bytes retained; rerun to download again.'
    fi
    mv "$archive.part" "$archive"
}

install_just() {
    local archive candidate listing
    select_release
    mkdir -p "$BIN_DIR" "$CACHE_DIR"
    archive="$CACHE_DIR/just-$JUST_VERSION-$TARGET.tar.gz"
    if [[ -f "$archive" ]] && ! verify_archive "$archive"; then
        mv "$archive" "$archive.rejected.$(date +%s).$$"
        printf 'Installer: cached Just archive failed SHA-256 verification; retained rejected bytes.\n' >&2
    fi
    [[ -f "$archive" ]] || download_archive "$archive"
    # Extract only the single regular `just` member to stdout. No archive path
    # can create files, symlinks, or traverse outside the install directory.
    listing=$(LC_ALL=C tar -tvzf "$archive" just) || fail 'Cannot inspect the Just archive.'
    [[ "$listing" = -* && "$listing" != *$'\n'* ]] || fail 'Just archive must contain one regular just binary.'
    candidate=$(mktemp "$BIN_DIR/just.XXXXXXXX")
    if ! tar -xOzf "$archive" just > "$candidate"; then
        fail "Could not extract Just; candidate retained at $candidate."
    fi
    chmod 755 "$candidate"
    compatible_just "$candidate" || fail "Downloaded Just cannot run on this host; candidate retained at $candidate."
    mv -f "$candidate" "$BIN_DIR/just"
    JUST="$BIN_DIR/just"
}

main() {
    local requested existing
    requested=${1:-install-resume}
    case "$requested" in
        resume|check|status|info) requested="install-$requested" ;;
        install-resume|install-check|install-status|install-info) ;;
        *) fail 'Usage: bash install.sh [install-resume|install-check|install-status|install-info]' ;;
    esac
    (( $# <= 1 )) || fail 'Pass only one installer command.'
    # Check Just before probing any application prerequisites.
    existing=$(command -v just || true)
    if [[ -n "$existing" ]] && compatible_just "$existing"; then
        JUST=$existing
    elif [[ -x "$BIN_DIR/just" ]] && compatible_just "$BIN_DIR/just"; then
        JUST="$BIN_DIR/just"
    else
        install_just
    fi
    [[ -f "$ROOT/justfile" ]] || fail 'The complete application folder (including justfile) is required.'
    PATH="$(dirname "$JUST"):$PATH"
    export PATH
    exec "$JUST" --justfile "$ROOT/justfile" "$requested"
}

main "$@"
