#!/bin/bash
# Pelican installation container: server files persist only under /mnt/server.
set -euo pipefail
export LC_ALL=C

readonly SERVER_DIR=/mnt/server
readonly REPOSITORY=ProjectReclaimer/project-reclaimer-releases
readonly API_BASE="https://api.github.com/repos/${REPOSITORY}"
readonly DOWNLOAD_BASE="https://github.com/${REPOSITORY}/releases/download"
readonly MINIMUM_VERSION=0.9.18

die() {
    printf 'Project Reclaimer installation failed: %s\n' "$*" >&2
    exit 1
}

# Compare canonical numeric version components without shell integer overflow.
version_at_least_minimum() {
    local candidate="${1#v}" minimum="$MINIMUM_VERSION" part
    local -a actual_parts minimum_parts
    candidate="${candidate%%[-+]*}"
    IFS=. read -r -a actual_parts <<< "$candidate"
    IFS=. read -r -a minimum_parts <<< "$minimum"
    for part in 0 1 2; do
        if (( ${#actual_parts[part]} > ${#minimum_parts[part]} )); then
            return 0
        elif (( ${#actual_parts[part]} < ${#minimum_parts[part]} )); then
            return 1
        elif [[ "${actual_parts[part]}" > "${minimum_parts[part]}" ]]; then
            return 0
        elif [[ "${actual_parts[part]}" < "${minimum_parts[part]}" ]]; then
            return 1
        fi
    done
    # A prerelease of the minimum version is older than that stable release.
    [[ "${1#v}" != "$MINIMUM_VERSION-"* ]]
}

validate_version() {
    local version="${1#v}" prerelease identifier version_without_build
    local semver='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$'
    [[ "$version" =~ $semver ]] || die "Version must be latest or a semantic version such as v${MINIMUM_VERSION}."
    version_without_build="${version%%+*}"
    if [[ "$version_without_build" == *-* ]]; then
        prerelease="${version_without_build#*-}"
        local -a identifiers
        IFS=. read -r -a identifiers <<< "$prerelease"
        for identifier in "${identifiers[@]}"; do
            if [[ "$identifier" =~ ^[0-9]+$ && ${#identifier} -gt 1 && "$identifier" == 0* ]]; then
                die "Numeric semantic-version prerelease identifiers cannot have leading zeros."
            fi
        done
    fi
    version_at_least_minimum "$version" || die "Dedicated-server releases before v${MINIMUM_VERSION} are unsupported."
}

requested_version="${RECLAIMER_VERSION:-v0.9.18}"
if [[ "$requested_version" != latest ]]; then
    validate_version "$requested_version"
    requested_version="v${requested_version#v}"
fi

packages=()
command -v curl >/dev/null 2>&1 || packages+=(curl)
command -v jq >/dev/null 2>&1 || packages+=(jq)
[[ -s /etc/ssl/certs/ca-certificates.crt ]] || packages+=(ca-certificates)
if (( ${#packages[@]} )); then
    command -v apt-get >/dev/null 2>&1 || die "The installer requires a Debian-based image with apt-get."
    apt-get update
    apt-get install -y --no-install-recommends "${packages[@]}"
fi
for required_command in sha256sum mktemp mkdir cp mv rm chmod; do
    command -v "$required_command" >/dev/null 2>&1 || die "Missing required installer command: ${required_command}."
done

mkdir -p "$SERVER_DIR"
stage_dir="$(mktemp -d "${SERVER_DIR}/.reclaimer-install.XXXXXX")"
marker_changed=0
committed=0
had_marker=0
cleanup() {
    local status=$?
    trap - EXIT
    if (( status != 0 && marker_changed && ! committed )); then
        if (( had_marker )); then
            mv -fT -- "$stage_dir/previous-version" "$SERVER_DIR/.reclaimer-version" || printf 'Could not restore the previous release-version marker.\n' >&2
        else
            rm -f -- "$SERVER_DIR/.reclaimer-version" || true
        fi
    fi
    if [[ "$stage_dir" == "$SERVER_DIR"/.reclaimer-install.* && -d "$stage_dir" ]]; then
        rm -rf -- "$stage_dir" || true
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

download() {
    curl --fail --show-error --silent --location \
        --proto '=https' --proto-redir '=https' --tlsv1.2 \
        --connect-timeout 30 --max-time 600 --retry 3 --retry-delay 2 \
        --output "$2" "$1"
}

if [[ "$requested_version" == latest ]]; then
    release_url="$API_BASE/releases/latest"
else
    release_url="$API_BASE/releases/tags/$requested_version"
fi
printf 'Resolving official Project Reclaimer release %s...\n' "$requested_version"
download "$release_url" "$stage_dir/release.json"
release_version="$(jq -er 'select(.draft == false) | .tag_name | select(type == "string" and length > 0)' "$stage_dir/release.json")" || die "GitHub did not return a valid published release."
validate_version "$release_version"
[[ "$release_version" == v* ]] || die "The release tag must use the official v-prefixed naming convention."
if [[ "$requested_version" != latest && "$release_version" != "$requested_version" ]]; then
    die "GitHub returned an unexpected release tag."
fi

asset_name="project-reclaimer-dedicated-${release_version}.exe"
asset_url="$(jq -er --arg name "$asset_name" '[.assets[] | select(.name == $name)] | if length == 1 then .[0].browser_download_url else error("Dedicated executable asset missing or ambiguous") end | select(type == "string")' "$stage_dir/release.json")" || die "The release does not contain exactly one ${asset_name}."
checksum_url="$(jq -er '[.assets[] | select(.name == "SHA256SUMS.txt")] | if length == 1 then .[0].browser_download_url else error("Checksum asset missing or ambiguous") end | select(type == "string")' "$stage_dir/release.json")" || die "The release does not contain exactly one SHA256SUMS.txt."
[[ "$asset_url" == "$DOWNLOAD_BASE/$release_version/$asset_name" ]] || die "The executable URL is outside the official release."
[[ "$checksum_url" == "$DOWNLOAD_BASE/$release_version/SHA256SUMS.txt" ]] || die "The checksum URL is outside the official release."

download "$checksum_url" "$stage_dir/SHA256SUMS.txt"
expected_hash="$(jq -Rrs --arg name "$asset_name" '
    split("\n")
    | map(rtrimstr("\r") | capture("^(?<hash>[A-Fa-f0-9]{64})[ \\t]+\\*?(?<name>.+)$")? | select(.name == $name))
    | if length == 1 then .[0].hash | ascii_downcase else error("Checksum entry missing or ambiguous") end
' "$stage_dir/SHA256SUMS.txt")" || die "No unique valid SHA-256 entry exists for ${asset_name}."

printf 'Downloading and verifying %s...\n' "$asset_name"
download "$asset_url" "$stage_dir/$asset_name"
[[ -s "$stage_dir/$asset_name" ]] || die "The dedicated executable download is empty."
printf '%s  %s\n' "$expected_hash" "$asset_name" > "$stage_dir/expected.sha256"
(cd "$stage_dir" && sha256sum --check --status expected.sha256) || die "The executable failed SHA-256 verification; existing files were preserved."
chmod 0644 "$stage_dir/$asset_name"
printf '%s\n' "$release_version" > "$stage_dir/new-version"

# Commit the version marker first and restore it if the final executable rename
# fails. Both staging files live on the server volume for atomic renames.
if [[ -e "$SERVER_DIR/.reclaimer-version" || -L "$SERVER_DIR/.reclaimer-version" ]]; then
    [[ ! -d "$SERVER_DIR/.reclaimer-version" ]] || die "The release-version marker is a directory."
    cp -p --no-dereference -- "$SERVER_DIR/.reclaimer-version" "$stage_dir/previous-version"
    had_marker=1
fi
mv -fT -- "$stage_dir/new-version" "$SERVER_DIR/.reclaimer-version"
marker_changed=1
mv -fT -- "$stage_dir/$asset_name" "$SERVER_DIR/project-reclaimer.exe"
committed=1
printf 'Installed Project Reclaimer %s. Existing game files and configuration were preserved.\n' "$release_version"
