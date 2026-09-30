# Installing Monguana

Monguana runs as a container. You need **Docker** or **Podman** — on Windows,
macOS or Linux — and nothing else: the image builds from this repository and
fetches everything it needs (Python packages, pytincture and the wapyt
widgetset) from the internet while it builds.

- **Disk:** about 1.5 GB for the images and build cache.
- **Network during the build:** github.com, pypi.org and deb.debian.org.
- **Time:** the first build takes roughly 3–10 minutes; later ones reuse most of it.

**Windows without Docker?** There is also a native installer, with no
container engine and no HTTPS: see
[section 11](#11-windows-without-docker-native-installer).

Every command below is shown for Docker. **For Podman, replace `docker` with
`podman`** — the compose files, flags and paths are the same. Where Windows
PowerShell differs from macOS/Linux shells, both are given.

**Contents**

1. [Install a container engine](#1-install-a-container-engine)
2. [Get Monguana](#2-get-monguana)
3. [Configure](#3-configure)
4. [Start it](#4-start-it)
5. [Connect to a MongoDB server](#5-connect-to-a-mongodb-server)
6. [Everyday tasks](#6-everyday-tasks): stop, logs, update, back up, reset a password, uninstall
7. [Without compose](#7-without-compose)
8. [Reaching Monguana from other machines](#8-reaching-monguana-from-other-machines)
9. [Troubleshooting](#9-troubleshooting)
10. [What was actually tested](#10-what-was-actually-tested)
11. [Windows without Docker (native installer)](#11-windows-without-docker-native-installer)

---

## 1. Install a container engine

Pick **one** engine. You also need its **compose** tool, which starts
Monguana from `compose.yaml` with one command.

### Windows 10 (22H2 or later) and Windows 11

**Docker Desktop**

1. Install Docker Desktop from <https://www.docker.com/products/docker-desktop/>,
   or in PowerShell: `winget install Docker.DockerDesktop`.
2. It uses WSL 2. If the installer asks, let it enable WSL, or run
   `wsl --install` in an administrator PowerShell and restart.
3. Start Docker Desktop and wait until it says it is running.
4. Check, in a new PowerShell window:
   ```powershell
   docker version
   docker compose version
   ```

**Podman**

1. Install Podman Desktop from <https://podman-desktop.io/>, or in PowerShell:
   `winget install RedHat.Podman-Desktop` (the command-line only package is
   `RedHat.Podman`).
2. Create and start the Podman machine (a small Linux VM, using WSL 2):
   ```powershell
   podman machine init
   podman machine start
   ```
   Podman Desktop offers to do this on first launch.
3. Install a compose provider. Podman Desktop's **Compose** extension installs
   one; on the command line: `winget install Docker.DockerCompose`.
4. Check:
   ```powershell
   podman version
   podman compose version
   ```

**Git** (to clone): `winget install Git.Git`, or skip it and download the ZIP
in step 2.

### macOS 13 or later (Apple Silicon or Intel)

**Docker Desktop** — install it from <https://www.docker.com/products/docker-desktop/>,
then start it and check `docker version` and
`docker compose version`. Docker-compatible alternatives such as OrbStack or
Colima work the same way, since they provide the `docker` command.

**Podman**

```sh
brew install podman docker-compose      # docker-compose is podman compose's provider
podman machine init
podman machine start
podman compose version
```

Podman Desktop (<https://podman-desktop.io/>) is the graphical alternative.

On Apple Silicon the image builds as native `arm64`; nothing to configure.

### Linux

**Docker Engine** — follow <https://docs.docker.com/engine/install/> for your
distribution and install the Compose plugin (`docker-compose-plugin`). To use
`docker` without `sudo`, add yourself to the `docker` group and log in again:

```sh
sudo usermod -aG docker "$USER"
```

**Podman** (rootless by default; nothing needs `sudo` once it is installed):

```sh
sudo apt install podman                 # Debian, Ubuntu
sudo dnf install podman                 # Fedora, RHEL, CentOS Stream
```

Then a compose provider — either one:

```sh
sudo apt install podman-compose         # or: pipx install podman-compose
sudo dnf install podman-compose
# or Docker's compose plugin, which podman compose also uses
```

Check with `podman compose version`.

SELinux (Fedora, RHEL) needs no extra steps: Monguana stores its data in a
named volume, not a host folder.

---

## 2. Get Monguana

```sh
git clone https://github.com/El-Iguana/monguana_wapyt.git
cd monguana_wapyt
```

Without Git: on <https://github.com/El-Iguana/monguana_wapyt> choose
**Code → Download ZIP**, unzip it, and open a terminal in the folder.

> **Windows:** the repository forces Unix line endings (`.gitattributes`), so
> Git's usual CRLF conversion does not touch it. Keep it that way — a `\r` at
> the end of a setting in `.env` becomes part of the value.

---

## 3. Configure

Copy the example settings:

```sh
cp .env.example .env                    # macOS, Linux
```
```powershell
Copy-Item .env.example .env             # Windows PowerShell
```

Open `.env` in any text editor and set, at least:

| Setting | What it does |
|---|---|
| `MONGUANA_ADMIN_PASS` | Password of the first account, `admin`. **Used only on the very first start**, when there are no accounts yet. Left empty, it is `change_me`. Either way, Monguana asks for a new password each time it loads until you change it from the app. |
| `MONGUANA_ADMIN_USER` | Its name, if not `admin`. |
| `MONGUANA_PORT` | The port on this computer, default `8766`. Change it if 8766 is taken. |

> **Windows:** Notepad may save the file as `.env.txt` when file extensions are
> hidden. In File Explorer turn on **View → Show → File name extensions** and
> check that the file is called exactly `.env`.

Everything else in `.env.example` is optional and explained there; the full
list is in [README.md](README.md#configuration).

---

## 4. Start it

```sh
docker compose up -d --build
```

This builds the image and starts the container in the background. Watch it
come up:

```sh
docker compose ps           # wait for "(healthy)"
docker compose logs -f      # Ctrl+C stops following, not the app
```

Then open **<http://127.0.0.1:8766/monguana>** (or your `MONGUANA_PORT`) and
sign in as `admin` with the password from `.env`. Change it from **Password**
in the toolbar.

> **Use `127.0.0.1`, not `localhost`.** Monguana (through pytincture) only
> serves signed-in sessions over plain HTTP on a *literal* loopback address,
> and it only listens on this computer. `localhost` is refused. To use it from
> other machines, see [section 8](#8-reaching-monguana-from-other-machines).

To also start a **MongoDB to try it with**:

```sh
docker compose --profile mongo up -d --build
```

Its connection details are in the next section.

---

## 5. Connect to a MongoDB server

In Monguana, **New connection** asks for a host and port (plus user,
password and auth database if the server needs them), or a full connection
string. Because Monguana runs inside a container, **"this computer" is not
`localhost` from its point of view**. Which host to type:

| Where MongoDB runs | Host | Notes |
|---|---|---|
| The bundled one (`--profile mongo`) | `mongo`, port `27017` | User `root`, password `MONGO_ROOT_PASSWORD` from `.env` (default `change-me-too`), auth database `admin`. |
| Installed on this computer — **Docker Desktop** (Windows, macOS) | `host.docker.internal` | Docker Desktop forwards it to this computer. |
| Installed on this computer — **Podman** (Windows, macOS) | `host.containers.internal` | `host.docker.internal` works too. |
| Installed on this computer — **Linux** | see [below](#mongodb-on-the-same-computer-linux) | |
| In another container | that container's name | After connecting it to Monguana's network: `docker network connect monguana_default <container>`. |
| Another machine, a server, MongoDB Atlas | its host name or IP, or the `mongodb+srv://…` string | Paste Atlas's string into **Connection string**. |

**Check before you guess.** This asks the container itself whether it can
reach a host and port:

```sh
docker compose exec monguana python manage.py probe host.docker.internal 27017
```

It answers *reachable*, *does not resolve*, or *not reachable* with the reason.
**Test connection** in the editor then checks the credentials as well.

### MongoDB on the same computer, Linux

On Linux, `host.docker.internal` / `host.containers.internal` reach this
computer's **network addresses**, not its `127.0.0.1`. A MongoDB listening
only on `127.0.0.1` — the default for many installs — answers *connection
refused* from the container. Three ways out, pick one:

1. **Host networking** (simplest): start Monguana with the second compose file.
   Inside it, `127.0.0.1` *is* this computer, so a connection with host
   `127.0.0.1` works. Monguana still listens on 127.0.0.1 only.
   ```sh
   docker compose down
   docker compose -f compose.host-network.yaml up -d --build
   ```
   Use the same `-f compose.host-network.yaml` on later `ps`, `logs`, `down`
   and `exec` commands. (Linux only: on Windows and macOS, host networking
   means the engine's VM.)
2. **Let MongoDB listen beyond loopback**, e.g. `bindIp: 0.0.0.0` in
   `mongod.conf`, then use `host.docker.internal` (Docker) or
   `host.containers.internal` (Podman). Only with authentication on and a
   firewall keeping the port off the network.
3. **Run MongoDB in a container** next to Monguana: the bundled
   `--profile mongo`, or your own container connected to `monguana_default`.

### tinymongo stores (no MongoDB server)

[tinymongo](https://pypi.org/project/tinymongo/) keeps MongoDB-style
databases in files: one SQLite or JSON file per database, in a folder.
Monguana opens those files itself, so **New connection → Connects to:
tinymongo** asks for a storage engine and a *store folder* instead of a host.

The folder is on the machine Monguana runs on, so connections may only open
folders inside one directory, the **tinymongo root**
(`MONGUANA_TINYMONGO_ROOT`):

| Install | tinymongo root |
|---|---|
| compose | `/tinymongo` in the container, on the `monguana-tinymongo` volume |
| Windows installer | `%LOCALAPPDATA%\Monguana\tinymongo` |
| `python service.py` | not set: tinymongo is off until you set it |

A store folder is typed relative to the root, and must already exist; leave
it blank for the root itself. `..`, absolute paths and symlinks out of the
root are refused.

To open files from this computer in the container, replace the volume line in
`compose.yaml` with a folder (`:z` for SELinux, as in section 6):

```yaml
      - ./tinymongo:/tinymongo:z
```

What tinymongo cannot do is not offered for its connections: statistics
show a document count only, and there is no Explain, Rename, capped or TTL
index, or hidden index. Indexes are ascending only.

**Dump, Restore and Copy to…** work on tinymongo too. A dump is a normal
mongodump-layout ZIP, so it restores into MongoDB (here, or with
`mongorestore`) and the other way round. **Copy database / collection to…**
(right-click) copies straight between any two of your connections — a MongoDB
server into a tinymongo store, or back — documents first, then the indexes,
as a job you can follow and cancel. The target may refuse some indexes (a
descending one, on tinymongo); the rest still copy, and the console says
which. One type does not survive a trip through tinymongo: a small
`NumberLong` comes back as a 32-bit integer.

**DuckDB and Parquet engines** are offered once their packages are installed
(large native wheels, so not by default). In the container, set this in
`.env` and rebuild with `docker compose up -d --build`:

```sh
MONGUANA_EXTRA_PACKAGES=tinymongo[duckdb] tinymongo[parquet]
```

For a local run: `uv sync --extra duckdb --extra parquet`. The Windows
installer has SQLite and JSON only.

### Other backends (plugins)

Packages can add further backends; `MONGUANA_EXTRA_PACKAGES` installs them in
the container the same way. `python manage.py backends` lists what is
installed and any plugin that failed to load. Writing one:
[docs/BACKEND_PLUGINS.md](docs/BACKEND_PLUGINS.md).

---

## 6. Everyday tasks

All of these run in the `monguana_wapyt` folder. With host networking, add
`-f compose.host-network.yaml` after `compose`.

**Stop, start, restart, logs**

```sh
docker compose stop
docker compose start
docker compose restart
docker compose logs -f
```

The container restarts with the engine unless you stopped it. (Podman on Linux
does not start containers at boot by itself; `podman generate systemd` or a
Quadlet does, see Podman's documentation.)

**Update to a newer version**

```sh
git pull
docker compose up -d --build
```

Your accounts, connections and layouts live in the `monguana-data` volume and
survive updates and rebuilds.

**Back up the data volume** — the SQLite database and `secret.key`, which
encrypts the stored MongoDB passwords. *Without `secret.key` the stored
passwords cannot be read.*

**Podman** (any OS) exports and imports volumes itself:

```sh
podman volume export monguana-data --output monguana-data.tar
# restore: stop Monguana, then import into the (existing, empty) volume
podman volume import monguana-data monguana-data.tar
```

**Docker** has no export, so a throwaway container copies the volume into a
dedicated `backups` folder:

```sh
# macOS, Linux
mkdir -p backups
docker run --rm -v monguana-data:/data -v "$PWD/backups:/backup:z" \
  docker.io/library/alpine tar czf /backup/monguana-data.tgz -C /data .
```
```powershell
# Windows PowerShell
New-Item -ItemType Directory -Force backups | Out-Null
docker run --rm -v monguana-data:/data -v "${PWD}\backups:/backup" `
  docker.io/library/alpine tar czf /backup/monguana-data.tgz -C /data .
```

To restore, stop Monguana, run the same command with
`tar xzf /backup/monguana-data.tgz -C /data`, and start it again.

> `:z` lets the container write to the folder on Linux with SELinux (Fedora,
> RHEL), where it is otherwise *Permission denied*; elsewhere it does nothing.
> It relabels the folder, so only ever give it a dedicated one like `backups`,
> never your home folder.

**Forgot the admin password**

```sh
docker compose exec -it monguana python manage.py reset-password admin
```

It asks for the new password twice. `manage.py users` lists the accounts;
`reset-password NAME --create` makes a new administrator.

**Uninstall**

```sh
docker compose down                 # removes the container, keeps your data
docker compose down -v              # …and deletes the data volume — irreversible
docker image rm localhost/monguana:latest
```

---

## 7. Without compose

The same thing with plain commands (Podman: replace `docker` with `podman`).

```sh
docker build -f Containerfile -t localhost/monguana:latest .
docker volume create monguana-data
docker run -d --name monguana --restart unless-stopped \
  -p 127.0.0.1:8766:8766 \
  --env-file .env \
  --add-host host.docker.internal:host-gateway \
  -v monguana-data:/data \
  localhost/monguana:latest
```
```powershell
docker build -f Containerfile -t localhost/monguana:latest .
docker volume create monguana-data
docker run -d --name monguana --restart unless-stopped `
  -p 127.0.0.1:8766:8766 `
  --env-file .env `
  --add-host host.docker.internal:host-gateway `
  -v monguana-data:/data `
  localhost/monguana:latest
```

- `-f Containerfile` is required with Docker, which otherwise looks for a file
  named `Dockerfile`.
- `--add-host …:host-gateway` gives Docker Engine on Linux the
  `host.docker.internal` name; it is harmless elsewhere.
- **Another host port** (say 9000): `-p 127.0.0.1:9000:8766` *and*
  `-e MONGUANA_CANONICAL_ORIGIN=http://127.0.0.1:9000`, since the app must know
  the address the browser uses.
- **Host networking on Linux**: replace the `-p` and `--add-host` lines with
  `--network host -e MONGUANA_BIND=127.0.0.1`.

---

## 8. Reaching Monguana from other machines

Monguana holds credentials for your databases, so it refuses to serve
sign-ins over plain HTTP anywhere but `127.0.0.1`. To use it from other
machines, put an HTTPS reverse proxy in front and tell Monguana its public
address. With [Caddy](https://caddyserver.com/) on the same machine, which
obtains the certificate itself:

1. In `.env`:
   ```
   MONGUANA_CANONICAL_ORIGIN=https://mongo.example.com
   MONGUANA_ALLOWED_HOSTS=mongo.example.com
   ```
2. A `Caddyfile`:
   ```
   mongo.example.com {
       reverse_proxy 127.0.0.1:8766
   }
   ```
3. `docker compose up -d` (to apply the new settings) and start Caddy.

Keep the `127.0.0.1:` in the published port: only the proxy should reach
Monguana. `mongo.example.com` must resolve to the machine, and ports 80 and 443
must reach Caddy for it to get a certificate (or use your own certificate with
Caddy's `tls` directive). Any HTTPS reverse proxy works the same way —
nginx, Traefik, a cloud load balancer — as long as it sends
`X-Forwarded-Proto: https`, which they do by default.

---

## 9. Troubleshooting

**The page says `Invalid host header`.** You opened `localhost` (or another
name). Use `http://127.0.0.1:8766` exactly, or set up
[section 8](#8-reaching-monguana-from-other-machines).

**`port is already allocated` / `address already in use`.** Something else
uses 8766. Set `MONGUANA_PORT=8777` (any free port) in `.env` and run
`docker compose up -d` again.

**`permission denied … docker.sock` (Linux).** Add yourself to the `docker`
group (section 1) and log in again, or use `sudo docker`.

**`Cannot connect to Podman` / `unable to connect to Podman socket`
(Windows, macOS).** The Podman machine is not running: `podman machine start`.

**`podman compose` says no compose provider was found.** Install
`docker-compose` or `podman-compose` (section 1).

**The build fails downloading something.** The build needs github.com,
pypi.org and deb.debian.org. Behind a proxy, configure it for your engine
(Docker Desktop: *Settings → Resources → Proxies*; Podman: `HTTP_PROXY` /
`HTTPS_PROXY` in the Podman machine or your environment) and build again.

**A connection times out or is refused.** Run the `manage.py probe` command
from [section 5](#5-connect-to-a-mongodb-server). *Refused* on Linux usually
means MongoDB listens on 127.0.0.1 only — see
[MongoDB on the same computer, Linux](#mongodb-on-the-same-computer-linux).

**`docker compose ps` shows `unhealthy`.** `docker compose logs monguana`
shows why; the last lines usually name the problem.

**Stored passwords stopped working after moving Monguana.** The data volume's
`secret.key` encrypts them; restoring the database without it loses them. Move
the whole volume (section 6), or re-enter the passwords.

---

## 10. What was actually tested

On Linux x86_64 with rootless Podman 5.8 and Docker Compose v5 as its compose
provider, starting each time from a clean copy of the repository and an empty
volume:

- `compose.yaml` with the bundled MongoDB (`--profile mongo`), and
  `compose.host-network.yaml` on another port against a MongoDB on the host's
  127.0.0.1 — each passing the full browser test suite (Playwright, Chromium):
  connections, queries, editing, indexes, the builders, export, dump and
  restore.
- `podman build` / `podman run` as in section 7, including a restart that
  keeps the data; `manage.py reset-password`; the healthcheck; the backups
  of section 6 (`podman volume export`/`import`, and the `tar` command, which
  needed `:z` under SELinux).
- The measured reason for the Linux section of step 5: from the bridge
  network, `host.containers.internal` reached a host service listening on all
  addresses and was refused by one on 127.0.0.1.
- The image **builds for `linux/arm64`** (Apple Silicon, ARM Linux) using
  prebuilt ARM wheels, and boots and signs in — checked under emulation.
- **Section 8** with Caddy (its internal CA, on port 8443): sign-in over HTTPS
  with Secure cookies, the healthcheck healthy in that mode, and plain HTTP to
  the container refused.
- Both compose files validate with Docker Compose (`docker compose config`).

On **Windows with Docker Desktop**, Monguana was installed and run by hand
(2026-09-29) and works.

Not tested here: Docker Engine itself (its daemon needed root on the test
machine), and macOS. The steps for those follow Docker's and
Podman's documented behaviour, notably that Docker Desktop and Podman's
machines forward `host.docker.internal` / `host.containers.internal` to the
computer running them. If something differs on your system, `manage.py probe`
will show where, and an issue on GitHub is welcome.

---

## 11. Windows without Docker (native installer)

For a computer without Docker: a normal Windows installer that runs Monguana
directly, for **you, on this computer only**. It needs no administrator rights,
no container engine and no certificate.

**Get it:** `Monguana-<version>-setup.exe`, attached to each
[release](https://github.com/El-Iguana/monguana_wapyt/releases) (built by the
`windows` GitHub Actions workflow). Windows 10 or 11, 64-bit; about 90 MB
installed.

**SmartScreen:** the installer is not code-signed yet, so Windows says
*"Windows protected your PC"*. Choose **More info → Run anyway**.

**What it does:**

- Installs to `%LOCALAPPDATA%\Programs\Monguana`, with its own Python. It
  does not touch any Python you have.
- Adds **Monguana**, **Reset Monguana password** and **Uninstall Monguana** to
  the Start menu. It can also add a desktop shortcut and start Monguana when
  you sign in.
- Starting **Monguana** runs the server in the background and opens
  `http://127.0.0.1:8766/monguana` in your default browser. If 8766 is taken,
  it uses the next free port. A tray icon offers **Open Monguana**, **Show log
  folder** and **Quit Monguana**. Starting it again while it runs just opens
  the browser.
- **Plain HTTP is safe here** because it listens on 127.0.0.1 only: nothing
  outside your computer can reach it. Use `127.0.0.1` exactly, not
  `localhost`, which answers *400 Invalid host header*.

**First start:** sign in as **`admin`** with password **`change_me`** (a
message box reminds you). Monguana then asks you to choose your own password,
and keeps asking each time it loads until you do; **Change password** in that
reminder, or the **Password** button, does it. Forgot it later? Run **Reset
Monguana password** from the Start menu.

**MongoDB is not included.** Point Monguana at your own server. A MongoDB on
the same computer is simply `127.0.0.1`, port `27017`: none of the container
networking of section 5 applies.

**Your data** (accounts, saved connections, the key that encrypts their
passwords, logs) is in `%LOCALAPPDATA%\Monguana`. Upgrading keeps it: run the
new installer over the old one. To back it up, quit Monguana from the tray and
copy that folder. Keep `secret.key` with `monguana.db`; without it, the saved
passwords cannot be decrypted.

**Uninstall** from Start menu → Uninstall Monguana, or Settings → Apps. It
asks whether to delete your data too; the default keeps it.

**If it does not start**, a message box says so. The reason is in
`%LOCALAPPDATA%\Monguana\logs\server.log` (tray → Show log folder). The
log from the start before that is `server.previous.log`.

**Command line** (PowerShell), for support or scripts:

```powershell
$m = "$env:LOCALAPPDATA\Programs\Monguana"
& "$m\python\python.exe" "$m\app\monguana_launcher.py" --status   # prints the URL if running
& "$m\python\python.exe" "$m\app\monguana_launcher.py" --stop
& "$m\python\python.exe" "$m\app\monguana_launcher.py" --check    # start, check the login page, stop
```

**Tested:** every build runs on a GitHub Actions Windows runner, which
installs it silently, starts it, checks the login page and the script MIME
types, stops it and uninstalls it. The installer from that build was also
installed and used by hand on **Windows 11** (2026-09-29), including the tray
icon, the first-run message box and the Start-menu shortcuts.

