#!/usr/bin/env bash
# Build the Turing desktop app and install it for the current user.
#
#   macOS: .app bundle -> /Applications/turing.app
#   Linux: AppImage    -> ~/.local/bin/turing.AppImage (+ XDG launcher/icon)
#
#   scripts/install-desktop.sh              # build + install
#   scripts/install-desktop.sh --no-build   # reinstall the last build
#   scripts/install-desktop.sh --open       # ... and launch it afterwards
#   scripts/install-desktop.sh --in-place   # self-update: don't quit the app
#
# No sudo, no notarization, no updater: the app is ad-hoc signed (macOS) and
# this just replaces the installed bundle in place.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

APP_NAME="turing.app"
BUNDLE_DIR="${REPO_ROOT}/desktop/src-tauri/target/release/bundle/macos"
SRC_APP="${BUNDLE_DIR}/${APP_NAME}"
DEST_DIR="/Applications"
DEST_APP="${DEST_DIR}/${APP_NAME}"

DO_BUILD=1
DO_OPEN=0
IN_PLACE=0

usage() {
    cat <<'EOF'
Usage: scripts/install-desktop.sh [--no-build] [--open] [--in-place]

  --no-build   skip `tauri build`; install whatever is already bundled
  --open       launch the installed app when done
  --in-place   don't quit a running instance; install over it. For the app's
               own in-app update flow: replacing a running bundle is safe on
               macOS (the running process keeps its inodes), and the caller
               relaunches itself when the script exits 0.
  -h, --help   this message
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-build) DO_BUILD=0 ;;
        --open) DO_OPEN=1 ;;
        --in-place) IN_PLACE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install-desktop: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# --- Linux ------------------------------------------------------------------
# Everything is per-user, XDG-shaped, and needs no sudo:
#   AppImage -> ~/.local/bin/turing.AppImage
#   launcher -> ${XDG_DATA_HOME:-~/.local/share}/applications/turing.desktop
#   icon     -> ${XDG_DATA_HOME:-~/.local/share}/icons/hicolor/128x128/apps/turing.png
# The macOS path below is untouched by this function.
install_linux() {
    local appimage_dir="${REPO_ROOT}/desktop/src-tauri/target/release/bundle/appimage"
    local bin_dir="${HOME}/.local/bin"
    local dest="${bin_dir}/turing.AppImage"
    local data_home="${XDG_DATA_HOME:-${HOME}/.local/share}"
    local build_started_at=""

    # --- build (same stale-bundle guards as the macOS path below) -----------
    if [[ "${DO_BUILD}" -eq 1 ]]; then
        echo "==> building (this compiles Rust; first run takes a few minutes)"
        # Only the AppImage: it is the one artifact this install needs; deb/rpm
        # are CI/deliverable formats. `build` chains the webui build via
        # tauri.conf.json's beforeBuildCommand.
        build_started_at="$(date +%s)"
        if ! npm --prefix "${REPO_ROOT}/desktop" run build -- --bundles appimage; then
            echo "install-desktop: build failed — not installing." >&2
            echo "  ~/.local was left untouched; fix the build and rerun." >&2
            exit 1
        fi
    else
        echo "==> skipping build (--no-build)"
    fi

    # Newest AppImage in the bundle dir (the name carries version + arch, e.g.
    # turing_0.1.0_amd64.AppImage, so it changes across versions).
    local candidate src_appimage=""
    for candidate in "${appimage_dir}"/*.AppImage; do
        [[ -f "${candidate}" ]] || continue
        if [[ -z "${src_appimage}" || "${candidate}" -nt "${src_appimage}" ]]; then
            src_appimage="${candidate}"
        fi
    done
    if [[ -z "${src_appimage}" ]]; then
        echo "install-desktop: no AppImage under ${appimage_dir}" >&2
        if [[ "${DO_BUILD}" -eq 0 ]]; then
            echo "  nothing has been built yet — rerun without --no-build." >&2
        fi
        exit 1
    fi

    # Same belt-and-braces as macOS: an AppImage older than the build we just
    # ran means the build produced nothing.
    if [[ "${DO_BUILD}" -eq 1 ]]; then
        local bundle_mtime
        bundle_mtime="$(stat -c %Y "${src_appimage}")"
        if [[ "${bundle_mtime}" -lt "${build_started_at}" ]]; then
            echo "install-desktop: ${src_appimage} predates this build — not installing." >&2
            exit 1
        fi
    fi

    # --- quit a running instance (skipped under --in-place, same rationale
    # as macOS: the in-app update flow runs this script from inside the app) --
    if [[ "${IN_PLACE}" -eq 0 ]] && pgrep -f "${dest}" >/dev/null 2>&1; then
        echo "==> stopping the running app"
        pkill -TERM -f "${dest}" 2>/dev/null || true
        for _ in $(seq 1 20); do
            pgrep -f "${dest}" >/dev/null 2>&1 || break
            sleep 0.5
        done
        if pgrep -f "${dest}" >/dev/null 2>&1; then
            echo "install-desktop: turing is still running and would not quit." >&2
            echo "  Quit it yourself and rerun with --no-build." >&2
            exit 1
        fi
    fi

    # --- install -------------------------------------------------------------
    echo "==> installing to ${dest}"
    mkdir -p "${bin_dir}" "${data_home}/applications" \
        "${data_home}/icons/hicolor/128x128/apps"
    # Unlink first: overwriting a running AppImage would hit ETXTBSY, while an
    # unlinked one keeps serving the old inode until relaunch (the --in-place
    # contract, same as macOS).
    rm -f "${dest}"
    install -m 755 "${src_appimage}" "${dest}"

    cp "${REPO_ROOT}/desktop/src-tauri/icons/128x128.png" \
        "${data_home}/icons/hicolor/128x128/apps/turing.png"
    cat >"${data_home}/applications/turing.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=turing
Comment=keyboard-first tiling operator surface for Turing
Exec=${dest}
Icon=turing
Terminal=false
Categories=Development;
StartupWMClass=turing
EOF
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "${data_home}/applications" >/dev/null 2>&1 || true
    fi

    local version
    version="$(basename "${src_appimage}")"
    version="${version#turing_}"
    version="${version%%_*}"

    if [[ "${DO_OPEN}" -eq 1 ]]; then
        echo "==> launching"
        ("${dest}" >/dev/null 2>&1 &)
    fi

    echo "done — turing ${version} installed at ${dest}"
    if [[ ":${PATH}:" != *":${bin_dir}:"* ]]; then
        echo "note: ${bin_dir} is not on your PATH — add it, or run the AppImage by full path."
    fi
}

case "$(uname -s)" in
    Darwin) ;; # fall through to the macOS path below
    Linux)
        install_linux
        exit 0
        ;;
    *)
        echo "install-desktop: unsupported OS $(uname -s) (macOS and Linux only)." >&2
        exit 1
        ;;
esac

# --- build ------------------------------------------------------------------
if [[ "${DO_BUILD}" -eq 1 ]]; then
    echo "==> building (this compiles Rust; first run takes a few minutes)"
    # Only the .app bundle: installing needs nothing else, and the dmg step is
    # slow and fails outright when a stale /Volumes/dmg.* mount is left behind
    # by an earlier interrupted build. `build` chains the webui build via
    # tauri.conf.json's beforeBuildCommand.
    #
    # The exit status is checked explicitly rather than left to `set -e`: a
    # build that fails *after* a previous bundle exists would otherwise let the
    # script sail on and reinstall the stale app, reporting success while the
    # change under test never shipped.
    build_started_at="$(date +%s)"
    if ! npm --prefix "${REPO_ROOT}/desktop" run build -- --bundles app; then
        echo "install-desktop: build failed — not installing." >&2
        echo "  /Applications was left untouched; fix the build and rerun." >&2
        exit 1
    fi
else
    echo "==> skipping build (--no-build)"
fi

if [[ ! -d "${SRC_APP}" ]]; then
    echo "install-desktop: no bundle at ${SRC_APP}" >&2
    if [[ "${DO_BUILD}" -eq 0 ]]; then
        echo "  nothing has been built yet — rerun without --no-build." >&2
    fi
    exit 1
fi

# Belt and braces on the same hazard: a bundle older than the build we just ran
# means the build produced nothing, so installing it would ship the last build
# under the impression it was this one.
if [[ "${DO_BUILD}" -eq 1 ]]; then
    bundle_mtime="$(stat -f %m "${SRC_APP}")"
    if [[ "${bundle_mtime}" -lt "${build_started_at}" ]]; then
        echo "install-desktop: ${SRC_APP} predates this build — not installing." >&2
        exit 1
    fi
fi

# --- quit a running instance ------------------------------------------------
# The executable is `turing-desktop`, not `turing`; match on the bundle path so
# this keeps working whatever the binary ends up being called.
#
# Skipped entirely under --in-place: that flag exists so the app can run this
# script on itself (the in-app update flow). Quitting would kill the pty this
# script is writing to, taking the script down with it mid-install.
app_running() { pgrep -f "${DEST_APP}/Contents/MacOS/" >/dev/null 2>&1; }

if [[ "${IN_PLACE}" -eq 0 ]] && app_running; then
    echo "==> quitting the running app"
    osascript -e 'tell application id "dev.owen.turing" to quit' >/dev/null 2>&1 \
        || osascript -e "quit app \"${DEST_APP}\"" >/dev/null 2>&1 \
        || true
    for _ in $(seq 1 20); do
        app_running || break
        sleep 0.5
    done
    if app_running; then
        echo "install-desktop: turing is still running and would not quit." >&2
        echo "  Quit it yourself (⌘Q, or Force Quit) and rerun with --no-build." >&2
        exit 1
    fi
fi

# --- install ----------------------------------------------------------------
if [[ ! -w "${DEST_DIR}" ]]; then
    echo "install-desktop: ${DEST_DIR} is not writable by $(whoami)." >&2
    echo "  Fix the permissions once, then rerun (no sudo needed afterwards):" >&2
    echo "    sudo chgrp admin ${DEST_DIR} && sudo chmod g+w ${DEST_DIR}" >&2
    echo "  Or install by hand:" >&2
    echo "    sudo rm -rf ${DEST_APP} && sudo cp -R '${SRC_APP}' ${DEST_DIR}/" >&2
    exit 1
fi

echo "==> installing to ${DEST_APP}"
mkdir -p "${DEST_APP}"
rsync -a --delete "${SRC_APP}/" "${DEST_APP}/"

# Copying invalidates the ad-hoc signature's seal on some macOS versions; the
# app refuses to launch until it is re-signed. Cheap enough to always redo.
codesign --force --deep --sign - "${DEST_APP}" >/dev/null 2>&1 \
    || { echo "install-desktop: codesign failed for ${DEST_APP}" >&2; exit 1; }

if xattr -p com.apple.quarantine "${DEST_APP}" >/dev/null 2>&1; then
    echo "==> clearing com.apple.quarantine"
    xattr -dr com.apple.quarantine "${DEST_APP}"
fi

VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' \
    "${DEST_APP}/Contents/Info.plist" 2>/dev/null || echo 'unknown')"

if [[ "${DO_OPEN}" -eq 1 ]]; then
    echo "==> launching"
    open "${DEST_APP}"
fi

echo "done — turing ${VERSION} installed at ${DEST_APP}"
