# TTC Positions Report

A desktop application for monitoring your Interactive Brokers positions with real-time market data, beautiful dark/light themes, and automatic updates.

![Version](https://img.shields.io/badge/version-2.0.0-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Mac-lightgrey)

## Features

- 📊 **Real-time positions** - View all your IBKR stock and options positions
- 🔄 **Auto-refresh** - Configurable refresh intervals (15s to 5min)
- 🌙 **Dark/Light mode** - Toggle with `D` key
- 🔍 **Quick search** - Filter symbols with `/` key
- 📈 **Market status** - Shows if market is open with countdown
- 🔔 **Auto-updates** - Get notified when new versions are available
- ⌨️ **Keyboard shortcuts** - `R` refresh, `E` export CSV, `?` help

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [GitHub Setup](#github-setup)
3. [Building the App](#building-the-app)
4. [Installing the App](#installing-the-app)
5. [Testing](#testing)
6. [Creating Updates](#creating-updates)
7. [Troubleshooting](#troubleshooting)

---

## Prerequisites

### On Your Development Machine

| Requirement | Windows | Mac |
|-------------|---------|-----|
| Python 3.11+ | [python.org](https://python.org) | `brew install python@3.11` |
| Git | [git-scm.com](https://git-scm.com) | `brew install git` |
| PyInstaller | `pip install pyinstaller` | `pip install pyinstaller` |
| GitHub CLI (optional) | [cli.github.com](https://cli.github.com) | `brew install gh` |

### Windows Only
- **Inno Setup 6** - [Download](https://jrsoftware.org/isdl.php) (for creating installer)

### Mac Only
- **Xcode Command Line Tools** - `xcode-select --install`

### On the Target Machine (Dad's Computer)
- **IBKR Trader Workstation (TWS)** - Must be running for the app to work
- TWS API must be enabled (see [IBKR API Setup](#ibkr-api-setup))

---

## GitHub Setup

### Step 1: Create a Repository

1. Go to [github.com/new](https://github.com/new)
2. Name it `ttc-positions` (or whatever you prefer)
3. Choose **Private** (recommended) or Public
4. Click **Create repository**

### Step 2: Push Your Code

```bash
# Navigate to project folder
cd /path/to/ttc_positions_report

# Initialize git (if not already)
git init

# Add all files
git add .

# Commit
git commit -m "Initial commit - TTC Positions Report v2.0.0"

# Add your GitHub repo as remote
git remote add origin https://github.com/YOUR_USERNAME/ttc-positions.git

# Push
git push -u origin main
```

### Step 3: Update App Configuration

Edit `ttc_positions_app.py` and update these lines (around line 50):

```python
GITHUB_OWNER = "YOUR_USERNAME"    # Your GitHub username
GITHUB_REPO = "ttc-positions"     # Your repository name
```

Commit and push this change:
```bash
git add ttc_positions_app.py
git commit -m "Configure GitHub for auto-updates"
git push
```

---

## Building the App

### Setting Up the Build Environment

**Both platforms:**
```bash
# Create virtual environment
python -m venv venv

# Activate it
# Windows:
venv\Scripts\activate
# Mac:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install pyinstaller
```

### Building on Windows

```batch
# Make sure venv is activated
venv\Scripts\activate

# Build the executable
pyinstaller --name "TTC Positions Report" ^
    --onefile ^
    --windowed ^
    --hidden-import=ib_async ^
    --hidden-import=webview ^
    --hidden-import=webview.platforms.winforms ^
    ttc_positions_app.py

# The exe will be in: dist\TTC Positions Report.exe
```

**Creating the Installer (requires Inno Setup):**

1. Open Inno Setup Compiler
2. Open `installer\ttc_setup.iss`
3. Click **Build → Compile**
4. Installer created at: `Output\TTC_Positions_Setup_2.0.0.exe`

**Or use the release script:**
```batch
release.bat
```

### Building on Mac

```bash
# Make sure venv is activated
source venv/bin/activate

# Build the app
pyinstaller --name "TTC Positions Report" \
    --onefile \
    --windowed \
    --hidden-import=ib_async \
    --hidden-import=webview \
    --hidden-import=webview.platforms.cocoa \
    ttc_positions_app.py

# The app will be in: dist/TTC Positions Report.app
```

**Creating a DMG:**
```bash
# Simple DMG
hdiutil create -volname "TTC Positions Report" \
    -srcfolder "dist/TTC Positions Report.app" \
    -ov -format UDZO \
    "dist/TTC_Positions_Report_2.0.0_Mac.dmg"
```

**Or use the release script:**
```bash
chmod +x release.sh
./release.sh
```

---

## Installing the App

### Windows Installation

1. **Get the installer**: `TTC_Positions_Setup_2.0.0.exe`
2. **Run it**: Double-click, click Next a few times
3. **Desktop shortcut**: Created automatically ✓
4. **Start Menu**: Added automatically ✓

**To run the app:**
- Double-click "TTC Positions Report" on the desktop

### Mac Installation

1. **Get the DMG**: `TTC_Positions_Report_2.0.0_Mac.dmg`
2. **Open it**: Double-click the DMG file
3. **Drag to Applications**: Drag the app to Applications folder
4. **First run**: Right-click → Open (to bypass Gatekeeper)

**To run the app:**
- Open from Applications or Spotlight (`Cmd+Space`, type "TTC")

---

## Testing

### IBKR API Setup

Before the app can connect to IBKR, you need to enable the API:

1. **Open Trader Workstation (TWS)**
2. **Go to**: File → Global Configuration → API → Settings
3. **Enable these settings**:
   - ☑️ Enable ActiveX and Socket Clients
   - ☑️ Read-Only API (recommended for safety)
   - Socket port: `7496` (or `7497` for paper trading)
4. **Add trusted IP**: `127.0.0.1`
5. **Click Apply and OK**

### Test the App

1. **Make sure TWS is running** and logged in
2. **Start the app** (double-click desktop icon)
3. **You should see**:
   - Your positions loading
   - Market status (Open/Closed)
   - Watchlist symbols

### Test Checklist

| Test | Expected Result |
|------|-----------------|
| App starts | Window opens, no errors |
| Data loads | Positions appear in table |
| Refresh works | Click refresh, data updates |
| Search works | Type symbol, table filters |
| Dark mode | Press `D`, colors invert |
| Export CSV | Press `E`, file downloads |
| TWS closed | Friendly error message |

---

## Creating Updates

When you want to release a new version:

### Step 1: Update Version Number

Edit `ttc_positions_app.py`:
```python
APP_VERSION = "2.1.0"  # Increment this
```

Also update in the HTML (search for `version-badge` and `TTC Positions Report v`).

### Step 2: Build the New Version

**Windows:**
```batch
release.bat
```

**Mac:**
```bash
./release.sh
```

### Step 3: Create GitHub Release

**Option A: Using GitHub CLI (recommended)**
```bash
# Login first (one-time)
gh auth login

# Create release
gh release create v2.1.0 \
    --title "v2.1.0" \
    --notes "What's new in this version" \
    "Output/TTC_Positions_Setup_2.1.0.exe"
```

**Option B: Using GitHub Website**
1. Go to your repo → **Releases** → **Create a new release**
2. Tag: `v2.1.0`
3. Title: `v2.1.0`
4. Description: What's new
5. **Attach files**:
   - Windows: `Output/TTC_Positions_Setup_2.1.0.exe`
   - Mac: `dist/TTC_Positions_Report_2.1.0_Mac.dmg`
6. Click **Publish release**

### Step 4: Users Get the Update

Next time users open the app:
1. They'll see a banner: "Update Available! Version 2.1.0 is ready."
2. They click "Update Now"
3. App downloads and installs automatically
4. App restarts with new version

---

## Troubleshooting

### "Please make sure Trader Workstation is running"

**Cause**: App can't connect to IBKR

**Fix**:
1. Make sure TWS is open and logged in
2. Check API is enabled (see [IBKR API Setup](#ibkr-api-setup))
3. Try restarting TWS
4. Click Refresh in the app

### "Windows protected your PC" (Windows)

**Cause**: Windows SmartScreen doesn't recognize the app

**Fix**:
1. Click "More info"
2. Click "Run anyway"
3. This only happens the first time

### "App is damaged" (Mac)

**Cause**: macOS Gatekeeper blocking unsigned app

**Fix**:
```bash
# Remove quarantine attribute
xattr -cr "/Applications/TTC Positions Report.app"
```

Or: Right-click → Open → Open

### App won't start

**Check the logs**:
- Windows: `C:\Users\YOU\AppData\Local\TTC Positions Report\log\`
- Mac: `~/Library/Logs/TTC Positions Report/`

Or in the app folder: `log/YYYY-MM-DD/ttc_positions_app.log`

### Update not showing

**Cause**: GitHub releases not configured correctly

**Check**:
1. `GITHUB_OWNER` and `GITHUB_REPO` are correct in `ttc_positions_app.py`
2. Release is published (not draft)
3. Release has the correct asset attached (installer/dmg)

---

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `R` | Refresh data |
| `/` | Focus search |
| `D` | Toggle dark mode |
| `C` | Toggle compact view |
| `E` | Export to CSV |
| `?` | Show shortcuts |
| `Esc` | Clear search / Close modal |

---

## File Structure

```
ttc_positions_report/
├── ttc_positions_app.py    # Main application
├── requirements.txt        # Python dependencies
├── ttc_watchlist.json      # Your watchlist (auto-created)
├── release.sh              # Mac build script
├── release.bat             # Windows build script
├── installer/              # Windows installer files
│   ├── ttc_setup.iss       # Inno Setup script
│   └── icon.svg            # App icon source
└── .gitignore              # Git ignore rules
```

---

## License

Private use only.

---

## Support

If something isn't working, check the [Troubleshooting](#troubleshooting) section or open an issue on GitHub.

