# Auto-update system: GitHub release check, verified download, and
# in-place self-replacement of the running executable on Windows.
#
# Windows flow: download new exe to %TEMP%, verify its SHA-256 against the
# SHA256SUMS.txt asset published with the release, then hand off to a batch
# helper that waits for this process to exit, swaps the exe at sys.executable
# (with retries, since Dropbox briefly locks files), and relaunches.

import hashlib
import json
import logging
import os
import platform
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# Use certifi's CA bundle when available (macOS dev setups often lack system
# certs in Python); Windows uses the OS certificate store.
try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()

GITHUB_OWNER = "bthomas530"
GITHUB_REPO = "ttc_positions_report"

# Override for testing the updater against a scratch repo: "owner/repo"
_repo_override = os.environ.get('TTC_UPDATE_REPO', '')
if '/' in _repo_override:
    GITHUB_OWNER, GITHUB_REPO = _repo_override.split('/', 1)

GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

CHECKSUMS_ASSET = 'SHA256SUMS.txt'
STABLE_WINDOWS_ASSET = 'TTC_Positions_Report_Windows.exe'

UPDATE_DIR_NAME = 'ttc_update'
FAIL_MARKER_NAME = 'update_failed.txt'
OLD_EXE_SUFFIX = '.old.exe'


def parse_version(version_str):
    """Parse version string into tuple for comparison"""
    try:
        v = str(version_str).lstrip('v')
        parts = v.split('.')
        return tuple(int(p) for p in parts[:3])
    except Exception:
        return (0, 0, 0)


def _update_dir():
    path = os.path.join(tempfile.gettempdir(), UPDATE_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def _http_get(url, user_agent, timeout=10):
    request = urllib.request.Request(url, headers={'User-Agent': user_agent})
    return urllib.request.urlopen(request, timeout=timeout, context=_SSL_CONTEXT)


def select_asset(assets, system=None):
    """Pick the right release asset for this platform.
    Windows prefers the stable-named exe, then any setup exe, then any exe."""
    system = system or platform.system()
    if system == 'Windows':
        for asset in assets:
            if asset.get('name') == STABLE_WINDOWS_ASSET:
                return asset
        for asset in assets:
            name = asset.get('name', '').lower()
            if 'setup' in name and name.endswith('.exe'):
                return asset
        for asset in assets:
            if asset.get('name', '').lower().endswith('.exe'):
                return asset
    else:
        for asset in assets:
            name = asset.get('name', '').lower()
            if name.endswith('.dmg') or name.endswith('.zip'):
                return asset
    return None


def check_for_updates(app_version, user_agent):
    """Check GitHub for available updates (non-blocking)"""
    try:
        logger.info('Checking for updates...')
        with _http_get(GITHUB_API_URL, user_agent, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))

        latest_version = data.get('tag_name', '0.0.0')
        if parse_version(latest_version) <= parse_version(app_version):
            logger.info(f'App is up to date (v{app_version})')
            return {'available': False, 'current_version': app_version}

        logger.info(f'Update available: {app_version} -> {latest_version}')
        assets = data.get('assets', [])
        asset = select_asset(assets)
        checksums_url = None
        for a in assets:
            if a.get('name') == CHECKSUMS_ASSET:
                checksums_url = a.get('browser_download_url')
                break

        return {
            'available': True,
            'current_version': app_version,
            'latest_version': latest_version,
            'download_url': asset.get('browser_download_url') if asset else None,
            'asset_name': asset.get('name') if asset else None,
            'checksums_url': checksums_url,
            'release_notes': data.get('body', ''),
            'release_url': data.get('html_url', ''),
        }

    except urllib.error.URLError as e:
        logger.warning(f'Could not check for updates (network error): {e}')
        return {'available': False, 'error': 'network',
                'message': "Couldn't check for updates. No worries, we'll try again next time."}
    except Exception as e:
        logger.warning(f'Could not check for updates: {e}')
        return {'available': False, 'error': 'unknown', 'message': str(e)}


def parse_checksums(text):
    """Parse sha256sum-format lines ('<hex>  <filename>') into {filename: hex}."""
    checksums = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and len(parts[0]) == 64:
            filename = parts[-1].lstrip('*')
            # CI may list paths like dist/foo.exe; keep just the basename
            checksums[os.path.basename(filename)] = parts[0].lower()
    return checksums


def sha256_of_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify_download(download_path, asset_name, checksums_url, user_agent):
    """Verify a downloaded asset against the release's SHA256SUMS.txt.
    Returns True on match. Releases without checksums (pre-2.2.0) fail closed."""
    if not checksums_url:
        logger.error('Release has no SHA256SUMS.txt asset; refusing unverified update')
        return False
    try:
        with _http_get(checksums_url, user_agent) as response:
            checksums = parse_checksums(response.read().decode('utf-8'))
    except Exception as e:
        logger.error(f'Could not fetch checksums: {e}')
        return False

    expected = checksums.get(asset_name)
    if not expected:
        logger.error(f'No checksum listed for {asset_name}')
        return False

    actual = sha256_of_file(download_path)
    if actual != expected:
        logger.error(f'Checksum mismatch for {asset_name}: expected {expected}, got {actual}')
        return False
    logger.info(f'Checksum verified for {asset_name}')
    return True


def download_update(download_url, asset_name, user_agent):
    """Download update file to the updater temp directory"""
    try:
        logger.info(f'Downloading update: {asset_name}')
        download_path = os.path.join(_update_dir(), asset_name)

        with _http_get(download_url, user_agent, timeout=30) as response:
            total_size = int(response.headers.get('content-length', 0))
            downloaded = 0
            last_logged_decile = -1

            with open(download_path, 'wb') as f:
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        decile = (downloaded * 10) // total_size
                        if decile > last_logged_decile:
                            last_logged_decile = decile
                            logger.info(f'Download progress: {decile * 10}%')

        logger.info(f'Download complete: {download_path}')
        return download_path

    except Exception as e:
        logger.error(f'Download failed: {e}')
        return None


def build_update_script(pid, source_path, target_path, backup_path, fail_marker):
    """Build the batch helper that swaps the exe after this process exits."""
    return f'''@echo off
setlocal enableextensions enabledelayedexpansion
set "SRC={source_path}"
set "DST={target_path}"
set "BAK={backup_path}"

:waitloop
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto waitloop
)

copy /y "%DST%" "%BAK%" >nul 2>&1

set /a TRIES=0
:copyloop
copy /y "%SRC%" "%DST%" >nul 2>&1
if not errorlevel 1 goto success
set /a TRIES+=1
if !TRIES! geq 10 goto fail
timeout /t 2 /nobreak >nul
goto copyloop

:success
del /f /q "%SRC%" >nul 2>&1
start "" "%DST%"
goto cleanup

:fail
echo Update could not replace the application file. The previous version was kept. > "{fail_marker}"
start "" "%DST%"

:cleanup
(goto) 2>nul & del "%~f0"
'''


def install_update(installer_path, cleanup_callback=None):
    """Apply the downloaded update and exit the app.

    Windows (frozen): stage the batch helper and exit; the helper replaces the
    exe at sys.executable and relaunches. Mac: open the DMG for manual install.
    """
    try:
        logger.info(f'Installing update: {installer_path}')

        if platform.system() == 'Windows':
            if not getattr(sys, 'frozen', False):
                logger.error('Refusing self-update: not running as a frozen executable')
                return False

            target_path = sys.executable
            backup_path = target_path + OLD_EXE_SUFFIX
            fail_marker = os.path.join(os.path.dirname(target_path), FAIL_MARKER_NAME)
            script = build_update_script(
                os.getpid(), installer_path, target_path, backup_path, fail_marker
            )
            script_path = os.path.join(_update_dir(), 'apply_update.bat')
            with open(script_path, 'w', encoding='ascii', errors='replace') as f:
                f.write(script)

            creationflags = (
                getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                | getattr(subprocess, 'DETACHED_PROCESS', 0)
            )
            subprocess.Popen(['cmd', '/c', script_path],
                             creationflags=creationflags, close_fds=True)
        else:
            subprocess.Popen(['open', installer_path])

        logger.info('Update initiated, exiting current instance...')
        if cleanup_callback:
            try:
                cleanup_callback()
            except Exception as e:
                logger.warning(f'Cleanup before update raised: {e}')
        logging.shutdown()
        # Hard exit: this runs on a background thread, where sys.exit() would
        # only end the thread, leaving the PID alive and the update helper
        # waiting on it forever.
        os._exit(0)

    except Exception as e:
        logger.error(f'Install failed: {e}')
        return False


def check_post_update_state(app_dir):
    """Called on startup: report a failed update marker and clean old backups.
    Returns the marker text if the last update failed, else None."""
    marker = os.path.join(app_dir, FAIL_MARKER_NAME)
    message = None
    if os.path.exists(marker):
        try:
            with open(marker, 'r') as f:
                message = f.read().strip()
        except Exception:
            message = 'The last update could not be applied.'
        try:
            os.remove(marker)
        except Exception:
            pass
        logger.warning(f'Previous update failed: {message}')

    # Remove the pre-update backup left by a successful swap
    if getattr(sys, 'frozen', False):
        backup = sys.executable + OLD_EXE_SUFFIX
        if os.path.exists(backup):
            try:
                os.remove(backup)
                logger.info('Removed previous-version backup after successful update')
            except Exception:
                pass

    return message
