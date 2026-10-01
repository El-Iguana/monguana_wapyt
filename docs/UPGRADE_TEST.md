# Upgrade test checklist

A hand test that an existing install updates in place and keeps its data,
following [INSTALL.md §12](../INSTALL.md#12-updating). The GitHub Actions
Windows job only tests a fresh install, so updating over an earlier version
needs this. Run it before a release that changes the installer, the launcher
or the database, and at least once for the Windows installer (never done yet).

Record each run at the bottom: date, from → to, machine, result.

## Windows installer

**Machine:** Windows 10 22H2 or 11, 64-bit; a VM snapshot is ideal. You need
the **old** and the **new** `Monguana-<version>-setup.exe` from the
[releases](https://github.com/El-Iguana/monguana_wapyt/releases), and a
MongoDB the VM can reach (or use a tinymongo store, which needs nothing else).

Start from **2.0.0** at least once: 2.1.0 added a database column (each
connection's backend), so that path exercises the migration. 2.1.0 → newer is
the common one.

### 1. Set up the old version

- [ ] Install the old version. Tick **both** shortcut options (desktop, and
      start when I sign in).
- [ ] Sign in as `admin` / `change_me`, and change the password when asked.
- [ ] Create a **second account** under Users.
- [ ] Add a **connection with a password** (MongoDB with a user, or a
      connection string with credentials), test it, save it. The password is
      what proves `secret.key` survived.
- [ ] Add a **tinymongo** connection (2.1.0 or later) and put a document in a
      collection.
- [ ] Open a collection, **resize and reorder** a column, switch to the JSON
      view. This is per-user UI state in the database.
- [ ] Note what's in `%LOCALAPPDATA%\Monguana` (files and sizes) and the
      version in Settings → Apps.
- [ ] Leave Monguana **running** (tray icon visible), with a browser tab open.

### 2. Update

- [ ] Run the new installer. **Don't** uninstall or quit Monguana first.
- [ ] It offers the same folder (`%LOCALAPPDATA%\Programs\Monguana`) or doesn't
      ask, and finishes without a "files in use" or "close applications" error.
- [ ] The old tray icon is gone (the installer stopped the server).
- [ ] Let it start Monguana at the end ("Start Monguana now").

### 3. Check the new version

- [ ] The browser opens and the login page loads. Reload the old tab too: it
      should show the login page or the app, not a broken page.
- [ ] Sign in with the **changed** password. There is no `change_me` prompt.
- [ ] **About** shows the new version (2.2.0 or later), and the release check
      says "This is the latest release" (or "Could not check" offline, which is
      fine).
- [ ] The **second account** still exists and can sign in.
- [ ] The **password connection** connects without re-entering the password.
- [ ] The **tinymongo** connection still opens, with its document.
- [ ] The collection keeps its **column widths and order**.
- [ ] From 2.0.0: Edit on the old connection shows **Connects to: MongoDB**.
- [ ] Settings → Apps lists **one** Monguana, at the new version.
- [ ] The Start menu, desktop and sign-in shortcuts all start the new version.
      Sign out and back in for the sign-in one; it starts in the tray only.
- [ ] `%LOCALAPPDATA%\Monguana\logs\server.log` has no traceback.
- [ ] `%LOCALAPPDATA%\Monguana` still has `monguana.db` and `secret.key`.

### 4. Uninstall and reinstall (keeps data)

- [ ] Uninstall, and answer **No** to "Also delete your Monguana data?".
- [ ] `%LOCALAPPDATA%\Programs\Monguana` is gone; `%LOCALAPPDATA%\Monguana`
      is still there.
- [ ] Install the new version again: everything from step 3 is still there.

## Container (compose)

Linux with Podman or Docker; quicker, and worth a run when the image or the
database changes.

- [ ] `git checkout v<old>` in a clean clone; `.env` from `.env.example`;
      `docker compose up -d --build`.
- [ ] As in Windows step 1: change the admin password, add a second account, a
      password connection and a column layout.
- [ ] `git checkout v<new>` (or `git pull` on `main`), then
      `docker compose up -d --build`.
- [ ] As in Windows step 3: sign in, About version, accounts, the connection
      without re-entering its password, the layout.
- [ ] `docker compose logs monguana` has no traceback.
- [ ] Repeat with `compose.host-network.yaml` if that's what you use.

## Runs

| Date | From → to | Machine | Result |
|---|---|---|---|
| | | | |
