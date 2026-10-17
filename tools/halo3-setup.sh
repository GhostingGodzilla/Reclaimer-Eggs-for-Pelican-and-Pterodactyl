#!/bin/bash
# Gets the Halo 3 files a Project Reclaimer server needs from your own Steam copy of MCC
# and copies them into that server's folder on this Wings machine. Only those files are
# downloaded (about 1.5 GB), not the whole collection.
# Download tool: DepotDownloader 3.4.0 (github.com/SteamRE/DepotDownloader), fetched on the
# first run and checked against the checksums pinned below.
#
# Run it on the machine that runs Wings, as your normal user (it asks for sudo only to copy):
#   bash tools/halo3-setup.sh <server id>
# The server id is the short id in the panel's server URL (like 1a2b3c4d) or the full UUID.
# Needs: curl, python3, sha256sum.
set -euo pipefail

MCC_APP=976730
DD_VERSION=3.4.0
DD_URL=https://github.com/SteamRE/DepotDownloader/releases/download/DepotDownloader_$DD_VERSION/DepotDownloader-linux-x64.zip
DD_ZIP_SHA256=a999dec66b4850fc961bd50366696d23c2d0fad7b18790e6a5647b2f19097a53
DD_SHA256=d62a1721564bdb96bacd9285bb5f96180a45202e82a9f85c6a88e5e8ee5f992c
# Where the tool and the downloaded files are kept between runs.
TOOLS=${HALO3_TOOLS_DIR:-$HOME/.cache/halo3-tools}
STAGE=${HALO3_STAGE_DIR:-$HOME/halo3-mcc}
TOOL=$TOOLS/DepotDownloader-$DD_VERSION/DepotDownloader
# What `dedicated init --maps "Guardian"` copies, per projectreclaimer.dev/host.
NEEDED=(
    halo3/halo3.dll
    halo3/maps/shared.map
    halo3/maps/campaign.map
    halo3/maps/040_voi.map
    halo3/maps/guardian.map
    halo3/maps/info/guardian.mapinfo
)

# DepotDownloader draws its login QR code as white blocks, which comes out inverted on a dark
# terminal and is too tall for VS Code's terminal panel. This redraws each code black on white,
# two rows per line, and passes every other line through unchanged.
QR_FILTER=$(cat <<'EOF'
import sys
out = sys.stdout.buffer
def write(text):
    out.write(text.encode() + b"\n")
    out.flush()
def colour(dark):
    return "0;0;0" if dark else "255;255;255"
def draw(rows):
    grid = [[row[2 * c] == "█" for c in range(len(rows))] for row in rows]
    for r in range(0, len(grid), 2):
        line = ""
        for c in range(len(grid)):
            top, bottom = grid[r][c], (grid[r + 1][c] if r + 1 < len(grid) else False)
            # A same-coloured pair is a plain background cell, so VS Code's contrast fix can't recolour it.
            if top == bottom:
                line += "\x1b[48;2;%sm " % colour(top)
            else:
                line += "\x1b[38;2;%s;48;2;%sm▀" % (colour(top), colour(bottom))
        write(line + "\x1b[0m")
rows = None
for raw in sys.stdin.buffer:
    line = raw.decode("utf-8", "replace").rstrip("\r\n")
    if rows is not None:
        width = len(rows[0]) if rows else len(line)
        if set(line) <= {"█", " "} and len(line) == width and width >= 40 and width % 2 == 0:
            rows.append(line)
            if len(rows) == len(rows[0]) // 2:
                draw(rows)
                rows = None
            continue
        for row in rows:
            write(row)
        rows = None
    write(line)
    if "sign in with this QR code" in line:
        rows = []
EOF
)

usage() { echo "Usage: bash $0 <server id>   (the short id from the panel URL, like 1a2b3c4d, or the full UUID)"; exit 1; }

# Prints the downloaded copy of $1, ignoring case like Windows does.
staged() { find "$STAGE" -path "$STAGE/.DepotDownloader" -prune -o -type f -ipath "*/$1" -print -quit; }

# Prints each folder Wings may keep server files in: the one its config names, then the defaults.
volume_roots() {
    local cfg
    for cfg in /etc/pelican/config.yml /etc/pterodactyl/config.yml; do
        [ -r "$cfg" ] && awk '$1 == "data:" { gsub(/["\047]/, "", $2); print $2; exit }' "$cfg"
    done
    echo /var/lib/pelican/volumes
    echo /var/lib/pterodactyl/volumes
}

if [ "${1:-}" = "--copy-as-root" ]; then
    if [ "$(id -u)" -ne 0 ]; then echo "The copy step needs sudo."; exit 1; fi
    SERVER_ID=${2:-}
    STAGE=${3:-}
    [[ $SERVER_ID =~ ^[0-9a-f]{8}(-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?$ ]] || usage
    [ -d "$STAGE" ] || usage
    # A Project Reclaimer server's folder is the one holding reclaimer.py.
    mapfile -t found < <(volume_roots | sort -u | while read -r root; do
        for dir in "$root/$SERVER_ID" "$root/$SERVER_ID"-*; do
            [ -f "$dir/reclaimer.py" ] && readlink -f "$dir"
        done
    done | sort -u)
    if [ ${#found[@]} -ne 1 ]; then
        echo "Couldn't find exactly one Project Reclaimer server with id $SERVER_ID (found ${#found[@]})."
        echo "Check the id in the panel, and that the server has finished installing."
        exit 1
    fi
    DEST=${found[0]}
    for f in "${NEEDED[@]}"; do
        install -D -m 644 "$(staged "$f")" "$DEST/game/$f"
        echo "   copied game/$f"
    done
    chown -R --reference="$DEST/reclaimer.py" "$DEST/game"
    exit 0
fi

if [ "$(id -u)" -eq 0 ]; then echo "Run this without sudo: bash $0 <server id>"; exit 1; fi
SERVER_ID=${1:-}
[[ $SERVER_ID =~ ^[0-9a-f]{8}(-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?$ ]] || usage
for cmd in curl python3 sha256sum; do
    command -v "$cmd" >/dev/null || { echo "This needs $cmd; install it and run again."; exit 1; }
done
if [ "$(uname -m)" != x86_64 ]; then echo "This only supports x86-64 Linux (the Wine image the egg uses is x86-64 too)."; exit 1; fi

echo "== Checking the download tool"
if ! echo "$DD_SHA256  $TOOL" | sha256sum -c --quiet - >/dev/null 2>&1; then
    echo "   Getting DepotDownloader $DD_VERSION"
    mkdir -p "$TOOLS"
    zip=$TOOLS/DepotDownloader-$DD_VERSION.zip
    curl -fsSL -o "$zip" "$DD_URL"
    if ! echo "$DD_ZIP_SHA256  $zip" | sha256sum -c --quiet - >/dev/null 2>&1; then
        rm -f "$zip"
        echo "The downloaded tool doesn't match the pinned checksum; stopping."
        exit 1
    fi
    rm -rf "${TOOL%/*}"
    python3 -I -m zipfile -e "$zip" "${TOOL%/*}"
    rm -f "$zip"
    chmod +x "$TOOL"
    if ! echo "$DD_SHA256  $TOOL" | sha256sum -c --quiet - >/dev/null 2>&1; then
        echo "The unpacked tool doesn't match the pinned checksum; stopping."
        exit 1
    fi
fi

mkdir -p "$STAGE"
missing=()
for f in "${NEEDED[@]}"; do [ -n "$(staged "$f")" ] || missing+=("$f"); done

if [ ${#missing[@]} -gt 0 ]; then
    echo "== Downloading the Halo 3 files from your Steam account"
    echo "   Use the Steam account that owns Halo: The Master Chief Collection. How do you want to sign in?"
    echo "     1) Scan a QR code with the Steam app on your phone (no password needed)"
    echo "     2) Type your Steam account name and password"
    read -rp "   Choose 1 or 2: " how
    filelist=$(mktemp)
    trap 'rm -f "$filelist"' EXIT
    # Steam lists Windows paths with backslashes; match either kind of slash.
    cat > "$filelist" <<'EOF'
regex:(^|[/\\])halo3[/\\]halo3\.dll$
regex:(^|[/\\])halo3[/\\]maps[/\\](shared|campaign|040_voi|guardian)\.map$
regex:(^|[/\\])halo3[/\\]maps[/\\]info[/\\][^/\\]+\.mapinfo$
EOF
    # A failed login or a missing depot shows up in the file check below.
    case $how in
        1)
            echo "   In the Steam phone app, tap the Steam Guard shield, then scan the QR code below."
            echo "   If the code doesn't fit, make the terminal window taller."
            (cd "$STAGE" && "$TOOL" -app "$MCC_APP" -os windows -filelist "$filelist" -dir "$STAGE" -qr | python3 -I -c "$QR_FILTER") || true
            ;;
        2)
            echo "   Use your account name (the name you sign in to Steam with), not your profile name."
            echo "   Type the password by hand instead of pasting it. Nothing shows while you type."
            echo "   Your password isn't saved."
            read -rp "   Steam account name: " steam_user
            if [ -z "$steam_user" ]; then echo "No account name given; stopping."; exit 1; fi
            (cd "$STAGE" && "$TOOL" -app "$MCC_APP" -os windows -filelist "$filelist" -dir "$STAGE" -username "$steam_user") || true
            ;;
        *)
            echo "Run it again and choose 1 or 2."
            exit 1
            ;;
    esac
fi

echo "== Checking the downloaded files"
missing=()
for f in "${NEEDED[@]}"; do
    path=$(staged "$f")
    if [ -n "$path" ]; then
        printf '   ok       %-34s %s\n' "$f" "$(du -h "$path" | cut -f1)"
    else
        printf '   MISSING  %s\n' "$f"
        missing+=("$f")
    fi
done
if [ ${#missing[@]} -gt 0 ]; then
    echo
    echo "Some files are missing, so nothing was copied to the server. Check the messages above"
    echo "(a failed Steam sign-in, or an account that doesn't own MCC), then run it again."
    exit 1
fi

echo "== Copying them into the server (type your Linux password if asked)"
sudo bash "$(readlink -f "$0")" --copy-as-root "$SERVER_ID" "$(readlink -f "$STAGE")"

echo
echo "Done. Start the server in the panel."
