# Reclaimer Eggs for Pelican and Pterodactyl

An importable egg for the Project Reclaimer Halo 3 dedicated server, for Pelican
and Pterodactyl, using Pelican's existing Wine 10 image (it runs on Pterodactyl's
Wings too). The JSON embeds its installer, runtime adapter and default
configuration, so no custom image or script hosting is needed.

**Status:** installation fixtures, configuration generation and runtime lifecycle
tests are checked locally. The generated configuration is accepted by the real
v0.9.18 Windows dedicated executable. A live match on a Linux Wings node has
not been tested.

## Setup

1. Import the egg. Both files contain the same installer and runtime.
   - **Pelican:** Admin → Eggs → Import, and choose
     [egg-project-reclaimer.json](egg-project-reclaimer.json) (`PLCN_v3` format).
     If an older Pelican rejects that format, import the PTDL_v2 file below.
   - **Pterodactyl:** Admin → Nests → Import Egg, choose
     [dist/egg-project-reclaimer-ptdl-v2.json](dist/egg-project-reclaimer-ptdl-v2.json),
     and pick the nest to put it in (create one first, such as "Halo", if you
     want it on its own).
2. Create a server on an **x86_64 Linux Wings node**, select **Wine 10**, and
   allow installation to finish. Start with **2 GiB RAM** and **8 GiB disk** for
   multiplayer, with more disk for campaign/mods and more CPU for large games.
3. Assign a primary game allocation, for example **49176**, and a secondary
   discovery allocation, for example **49175**. Set **Discovery TCP port** to
   that secondary port. Allow UDP at the game port and TCP at the discovery port
   in the node/provider firewall. The panel supplies the game port automatically.
   Every server on a shared IP needs its own allocations.
4. Get the Halo 3 game files from your own Steam copy of MCC. No game files come
   with this egg, so you need a Steam account that owns Halo: The Master Chief
   Collection. Pick one way:

   **A. On the Wings machine, signing in to Steam with a QR code.** No Windows
   PC or upload needed. On the Linux machine that runs Wings, as your normal
   user (not root), run [tools/halo3-setup.sh](tools/halo3-setup.sh) with the
   server's id, which is the short id in the panel's server URL:

   ```sh
   bash tools/halo3-setup.sh 1a2b3c4d
   ```

   Choose **1** and scan the QR code with the Steam app on your phone (the
   Steam Guard shield). It downloads only the files the default **Guardian**
   map needs, about 1.5 GB, then asks for your sudo password to copy them into
   the server. It needs `curl`, `python3` and `sha256sum`, and fetches
   DepotDownloader 3.4.0, checked against a pinned checksum. It finds the
   server's folder through the Wings config on Pelican and Pterodactyl alike.
   Your Steam password is never saved. For other maps or the bundled playlist,
   use B.

   **B. On a Windows PC with MCC installed.** Download Project Reclaimer from its
   [official releases](https://github.com/ProjectReclaimer/project-reclaimer-releases/releases),
   verify its SHA-256 checksum, and rename the executable to
   `project-reclaimer.exe`. In PowerShell, for example:

   ```powershell
   .\project-reclaimer.exe dedicated init C:\reclaimer-upload --from "D:\SteamLibrary\steamapps\common\Halo The Master Chief Collection" --maps "Guardian"
   ```

   Change the MCC path to your installation. Upload the **`game` folder inside
   `C:\reclaimer-upload`** through the panel's SFTP or file manager, preserving
   its structure. Keep the egg's installed `dedicated.toml`; the Windows init
   command creates a configuration containing two servers.
5. Set the name, map, game mode and player limit under Startup, then start.
   First start creates a persistent Wine prefix. The panel marks the server ready
   after `dedicated check` passes and `dedicated status` reports it running.

The game files must end up at paths such as:

```text
game/halo3/halo3.dll
game/halo3/maps/shared.map
game/halo3/maps/<your-map>.map
game/halo3/maps/info/<your-map>.mapinfo
```

Both ways pick the correct internal map filenames for you and bring the
campaign files used by Forge's vehicles and AI. No game files are included in
this repository or downloaded by the egg itself.

## Joining the server

Players need a Windows PC with their own Steam copy of MCC (Halo 3 installed)
and the Project Reclaimer client on the **same release as the server** (the
Project Reclaimer release setting, `v0.9.18` by default). A different release
is refused, and the Server Browser shows the version in red.

- **Server Browser:** choose **Server Browser** in Project Reclaimer's main
  menu and look for the **Server name** set under Startup. The server reports
  itself to the project's public master servers, so players don't need to add
  anything.
- **Direct Connect:** join by address as `ADDRESS:GAME_PORT`. The game port is
  the server's primary allocation, shown on its Network page in the panel. The
  address is the node's public IP or a DNS name pointing at it.

**Hosting at home:** on your router, forward the game port (UDP) and the
discovery port (TCP) to the Wings machine. The Server Browser leaves out
servers that don't answer. Many home routers can't reach their own public
address from inside, so players on the same network as the server should
Direct Connect to the Wings machine's local IP and the game port instead.

## Settings and content

| Setting | Behavior |
| --- | --- |
| Project Reclaimer release | Defaults to `v0.9.18`; accepts newer release tags or `latest`. Reinstall after changing it. |
| Server name / Maximum players | Browser name and multiplayer limit, from 1 to 127 players. |
| Map / Game mode | Defaults to Guardian / Slayer. Ignored when a playlist or campaign is selected. |
| Playlist | Leave empty for one map/mode; otherwise use a path such as `playlists/slayer.playlist.json`. |
| Discovery TCP port | Must match an additional assigned allocation. Set by the panel administrator. |
| Public address | Empty detects the public address; set your IPv4 address or hostname for nodes with multiple public IPs. |
| RCON password | Empty disables RCON. At least eight characters enables it on TCP at the game port. |

To use the bundled three-map rotation, prepare `Guardian,Valhalla,The Pit`, upload
their game files, and set Playlist to `playlists/slayer.playlist.json`. Put custom
maps, game types and mods under `content/Maps`, `content/Game Modes` and
`content/Mods`. Automatic Steam Workshop provisioning is not included; upload
Workshop content yourself.

Edit `dedicated.toml` for advanced settings such as admins, welcome messages,
anti-cheat, VPN blocking and mod sharing. Keep **one `[[server]]` block**. The
adapter preserves advanced values and writes `dedicated.pelican.toml` beside
the template on each start. Relative paths remain relative to the server root.
Panel controls override the fields listed above; UPnP and executable auto-update
are disabled. Restart after changing the template or Startup settings. Playlist
and content changes can be reloaded by Project Reclaimer while it runs.

For campaign hosting, replace the map/game entries in the template's server
block with a `campaign` table from the upstream hosting guide, clear Playlist,
upload campaign files, and keep Maximum players at 16 or fewer.

RCON listens on every container interface so tools can reach its allocated TCP
port. It is unencrypted: restrict it to trusted tools with the provider/node
firewall or use a private network. RCON and discovery must use different TCP
ports when RCON is enabled. The panel console shows server output; use RCON
for moderation commands.

## Updates and troubleshooting

Change the release setting and reinstall to install a checksum-verified server
executable. Reinstallation updates the adapter and executable while preserving
your game files, advanced template, playlists, bans and Wine prefix. Back up the
server first. Players and server must use the same Project Reclaimer release.

- **Missing game files:** check the paths above. Upload `game/` into the server
  root, avoiding a nested `game/game` or `reclaimer-upload/game` folder.
- **Two server blocks:** restore the egg template or remove the extra block from
  an uploaded Windows configuration. Create another panel server for a second
  instance.
- **Can't find/join it:** check allocations, firewall rules and Public address.
  From another network, run `project-reclaimer.exe server-status PUBLIC_IP:GAME_PORT`.
- **Startup stays in progress:** inspect the console and `data/` logs. The
  adapter deliberately waits for a successful status check before reporting ready.
- **Stopping:** the panel sends Ctrl+C; the adapter forwards SIGINT to the Wine
  process group and allows 25 seconds for shutdown before escalation.

## Development

Requires Python 3.11+ to build and run the offline tests:

```sh
python tools/build_egg.py
python -m unittest discover -s tests -v
bash -n scripts/install.sh
bash -n tools/halo3-setup.sh
```

On Windows, `uv run --python 3.13 python ...` can be used for the
Python commands. Installer fixtures require Bash and jq; `JQ_BINARY` can point
to a jq executable. POSIX process-group tests require Linux.

Edit `runtime/reclaimer.py`, `scripts/install.sh` or `config/`, then rebuild
both egg exports. Do not edit the embedded copies in the generated JSON.

References: [Project Reclaimer hosting](https://projectreclaimer.dev/host),
[official releases](https://github.com/ProjectReclaimer/project-reclaimer-releases/releases),
[Pelican Wine image](https://github.com/pelican-eggs/yolks/tree/master/wine/10),
[Pelican egg importer](https://github.com/pelican/panel/blob/v1.0.0-rc.1/app/Services/Eggs/Sharing/EggImporterService.php).

## License

[MIT](LICENSE). No Halo game files are included; you need your own copy of MCC.
