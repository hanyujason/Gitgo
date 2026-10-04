# Terminal release boundary

Gitgo v1 is one terminal product with one public command. The installed
dashboard may start an internal Native Host process, but the Host is not a
second user-facing application and is versioned atomically with the dashboard.

The installer adds its installation directory to the current user's `PATH`, so
`gitgo` works in an existing terminal. A shortcut/double-click launcher opens
the terminal selected in the user configuration; an attached TTY or SSH
session is always reused. The final public command and optional aliases come
from `product.json`, so packaging does not freeze the eventual product name in
runtime code.

The launcher reads `~/.gitgo/config.json` before starting the Host. Its stable
JSON shape is:

```json
{
  "launcher": {
    "terminal": "auto",
    "command": "",
    "args": []
  }
}
```

`terminal` accepts `auto`, `current`, `windows_terminal`, or `custom`. `auto`
prefers Windows Terminal only for Explorer/shortcut launches. `current` always
keeps the console Windows assigned to the executable. A custom terminal runs
`command` with `args`, followed by `gitgo.exe --attached`; for example WezTerm
can use `{"terminal":"custom","command":"wezterm.exe","args":["start","--"]}`.
Changing this file is picked up by a running Host for runtime settings; launcher
selection naturally applies on the next product launch.

The Windows installer also registers the primary executable under the current
user's `App Paths`. PATH removal is segment-based and removes only the exact
installation directory; it never restores an old whole-PATH snapshot over
changes made by other applications. Inno Setup supplies the dedicated
uninstaller. `build_windows.ps1 -BuildInstaller` creates it when an Inno Setup
compiler is available; staging the product does not silently install one.

The uninstaller owns only the installation and user configuration/credential
files. Runtime/session databases are retained for recovery. It must never
traverse configured workspace paths, remove repositories, delete worktrees, or
remove a project's `.gitgo`/`.git/gitgo` directory. Automatic update is
intentionally out of scope for the first installer.

Platform packages are built independently. Windows uses an `.exe` launcher and
installer; macOS and Linux expose the same command/protocol/config contracts but
use native package and credential-store adapters.

## macOS package and installer

`packaging/build_macos.sh` builds one architecture at a time on macOS. It
compiles the Bun Dashboard as the public `gitgo` command, freezes the Python
Native Host with PyInstaller, performs the packaged Host/Daemon/tool-runner
smoke test, applies ad-hoc code signatures by default, and creates a
`dist-installer/gitgo-macos-<architecture>.tar.gz` archive plus its SHA-256
file. A Developer ID identity can be supplied for a publishable signed build;
Apple notarization remains a separate credentialed release step.

After extracting the archive, run `./install.sh`. The installer is per-user and
does not use `sudo`: payload files go under
`~/Library/Application Support/Gitgo/app`, while `~/.local/bin/gitgo` is an
owned symlink. The installer adds one marked PATH block to the active zsh or
bash profile, or a dedicated fish `conf.d` file. Re-running it performs an
atomic upgrade with rollback.

Run the installed `uninstall.sh` to remove the application. It removes only
Gitgo-owned command links, marked PATH entries, global configuration, and
provider credentials (including native Keychain items). Runtime databases and
all project directories, repositories, worktrees, and project `.gitgo` data
are preserved. `--keep-config` keeps global configuration and credentials too.

`packaging/release_macos.sh` is the fail-closed release entry point. It mirrors
the Windows privacy, test, clean-tree, commit-message, and build gates and never
pushes or installs anything.

## Linux package and installer

`packaging/build_linux.sh` builds one native architecture at a time on Linux.
The maintained CI target is `linux-x86_64`; the script also accepts native
`aarch64` builders. It compiles the Bun Dashboard, freezes the Python Native
Host with PyInstaller, exercises the packaged Host/Daemon/tool-runner, and
creates `dist-installer/gitgo-linux-<architecture>.tar.gz` together with a
SHA-256 file. Linux executables are always built on Linux rather than
cross-compiled from macOS.

After extracting the archive, run `./install.sh`. The per-user installer does
not use `sudo`: it installs under `${XDG_DATA_HOME:-~/.local/share}/gitgo/app`,
creates owned command links in `~/.local/bin`, and adds one marked PATH block
for bash or zsh (or a fish `conf.d` file). Re-running it is an atomic upgrade
with rollback. The installed `uninstall.sh` removes only Gitgo-owned links,
PATH entries, global configuration, credentials, and application files. XDG
runtime databases and every project/repository remain untouched.

Provider credentials use the desktop Secret Service through `secret-tool` and
are represented on disk only by opaque references. Debian/Ubuntu users can
install the client with `sudo apt install libsecret-tools`; a running Secret
Service such as GNOME Keyring or KWallet is also required. Gitgo fails closed
instead of writing plaintext when the service is unavailable.

`packaging/release_linux.sh` applies the same clean-tree, commit-message,
privacy, Python test, Dashboard test, build, and packaged-runtime gates as the
other platforms. `.github/workflows/linux-package.yml` runs those gates on an
Ubuntu x64 runner, verifies a clean install and safe uninstall, and publishes
the archive as a workflow artifact. CI builds the pinned SQLite source in
`scripts/prepare_linux_sqlite.py` and verifies its official SHA3-256 before
packaging, because common Linux Python distributions may still link an SQLite
version rejected by Gitgo's WAL safety guard.

## Release build prerequisites

`build_windows.ps1` selects `GITGO_PYTHON`, `~/.gitgo/runtime/python`, or the
per-user LocalAppData Gitgo runtime, in that order. The selected build runtime
must pass Gitgo's SQLite WAL-safety guard. PyInstaller may be installed in that
runtime or supplied as a separate build-only package directory with
`-PyInstallerPackages`; the script also recognizes the runtime's inactive
`packages/` build directory. It fails closed instead of falling back to a
system Python with an unsafe SQLite build. Bun is resolved from
`~/.bun/bun.exe` unless passed explicitly.

The compiled Native Host is smoke-tested through the versioned stdio protocol
before publication. Release artifacts remain under ignored `dist-terminal/`
and `dist-installer/` directories; executables and runtime databases are never
committed to Git.

The repository-root `build.py` is retained only to archive the early Qt Git
manager. It requires an explicit `--legacy-qt` flag and is not part of the
terminal release pipeline.

## One-command release verification

Run `packaging/release_windows.ps1` from PowerShell for the maintained release
flow. It fails closed on a dirty tree, invalid commit messages, an unsafe
SQLite runtime, tracked secrets/private state, Python or Dashboard test
failures, and build failures. By default it then stages the Windows product;
`-VerifyOnly` performs every check without packaging, while `-BuildInstaller`
also invokes Inno Setup when its compiler is already installed.

The script never pushes, installs, rewrites Git history, downloads build tools,
or deletes project/runtime data. `-AllowDirty` exists only for a local rehearsal
and should not be used as release evidence.

Run `packaging/release_macos.sh` on macOS or `packaging/release_linux.sh` on
Linux for their equivalent fail-closed verification and native package build.
