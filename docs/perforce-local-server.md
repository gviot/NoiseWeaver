# A local, backed-up Perforce server for the assets repo

NoiseWeaver's storage layer can point an `assets` repo at a Perforce stream. This is how to stand up
a **local, password-protected Helix Core server** on a workstation and make it safe to back up with a
**folder backup tool (Backblaze, Time Machine, …)**. The key thing most people get wrong: **you cannot
fold-back-up a live Perforce server** — the `db.*` files are a live database; a copy taken mid-write is
unrestorable. The fix is scheduled **checkpoints**.

> Staging does **not** go here — it's an immutable content-addressed store, so it's the one thing you
> *can* back up as a plain folder. Perforce is for `assets` (the versioned source/output).

## 1. Install (macOS, Homebrew)

```bash
brew install --cask perforce      # p4 (client) + p4d (server)
```

## 2. Server: directories + a launchd agent

```bash
mkdir -p ~/Perforce/server/checkpoints ~/Perforce/logs
```

`~/Library/LaunchAgents/org.perforce.p4d.plist` runs `p4d` bound to **loopback only** (not the LAN) and
restarts it on crash/login:

```
p4d -r ~/Perforce/server -p localhost:1666 -L ~/Perforce/logs/p4d.log
```

`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.perforce.p4d.plist`

## 3. Secure it + provision the depot

```bash
export P4PORT=1666 P4USER=$(whoami)          # bare 1666 resolves to localhost
pw='<a strong password>'                      # keep it in your app's .env as P4PASSWD

printf '%s\n%s\n' "$pw" "$pw" | p4 passwd     # set the password (fresh server)
printf '%s\n' "$pw" | p4 login                # get a ticket
p4 configure set security=3                    # passwords required (level 3, not 4 — see note)

# a group with an unlimited ticket so the backup job stays authenticated without a stored password
printf 'Group: nw-local\nTimeout: unlimited\nUsers:\n\t%s\n' "$P4USER" | p4 group -i
printf '%s\n' "$pw" | p4 login                 # re-login → effectively-unlimited ticket

# checkpoints + rotated journals land under checkpoints/
p4 configure set journalPrefix=$HOME/Perforce/server/checkpoints/p4d

# store binaries as binary (Perforce text-detection corrupts them otherwise)
printf 'TypeMap:\n\tbinary //....png\n\tbinary //....fbx\n\tbinary //....glb\n\tbinary //....exr\n\tbinary //....wav\n\tbinary //....zip\n' | p4 typemap -i

# a stream depot + a mainline stream for the assets repo
printf 'Depot: assets\nOwner: %s\nType: stream\nStreamDepth: //assets/1\nMap: assets/...\n' "$P4USER" | p4 depot -i
printf 'Stream: //assets/main\nOwner: %s\nName: main\nParent: none\nType: mainline\nParentView: noinherit\nPaths:\n\tshare ...\n' "$P4USER" | p4 stream -i
```

> **security=3, not 4.** Level 4 requires `$P4PASSWD` to hold a *ticket*, not a password — but the
> storage layer keeps the password in `$P4PASSWD` to log in, so level 4 would reject every call. Level
> 3 (strong passwords + tickets required) is the right setting when the password lives in the env.

NoiseWeaver creates the *client workspace* itself (`PerforceRepo.ensure_ready`), so you don't make one
by hand — you only provide the depot + stream above.

## 4. Backups: scheduled checkpoints + a folder backup

A daily `~/Perforce/checkpoint.sh` asks the **running** server for a consistent snapshot and rotates the
journal (using the unlimited ticket, no password needed):

```bash
p4 -p localhost:1666 -u "$(whoami)" admin checkpoint -Z   # -Z gzips it
# + prune to the newest N checkpoints/journals
```

A launchd agent (`org.perforce.p4d-checkpoint.plist`, `StartCalendarInterval` 03:00) runs it nightly.

Then point **Backblaze at `~/Perforce/server`**. It contains the checkpoints, rotated journals, and the
depot archive files — everything needed to restore. The live `db.*` are included too but ignored on
restore. (Backblaze can't be scripted here; add the folder in its app once.)

### Restore

On a fresh machine: install p4d, recreate `~/Perforce/server`, then replay the newest checkpoint and
drop the depot archives back in:

```bash
p4d -r ~/Perforce/server -jr ~/Perforce/server/checkpoints/p4d.ckp.<N>.gz   # rebuilds db from the snapshot
# (the depot archive dirs, e.g. ~/Perforce/server/assets/, come from the same backup)
```

## 5. Point NoiseWeaver at it

In `noiseweaver.toml`, with `P4PASSWD` in your environment (`.env`):

```toml
[repos.assets]
kind   = "perforce"
port   = "localhost:1666"
user   = "you"
stream = "//assets/main"
client = "assets-<host>-you"   # NoiseWeaver creates/updates this workspace
root   = "~/p4/assets"
```
