#!/bin/bash
# TTC Positions Report - Mac Release Script
# Creates a new release build and optionally uploads to GitHub
#
# Usage:
#   ./release.sh              # Build and prompt for release
#   ./release.sh --build-only # Just build, don't create release
#   ./release.sh 2.0.1        # Build with specific version
#
# Requirements:
#   - PyInstaller (pip install pyinstaller)
#   - GitHub CLI (optional, for automated release): brew install gh

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Project settings
APP_NAME="TTC Positions Report"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIST_DIR="$SCRIPT_DIR/dist"
BUILD_DIR="$SCRIPT_DIR/build"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  TTC Positions Report - Release Tool  ${NC}"
echo -e "${BLUE}========================================${NC}"
echo

# Get version from app or command line
if [ -n "$1" ] && [ "$1" != "--build-only" ]; then
    VERSION="$1"
else
    # Extract version from ttc_positions_app.py
    VERSION=$(grep 'APP_VERSION = ' ttc_positions_app.py | cut -d'"' -f2)
fi

echo -e "${YELLOW}Version: ${VERSION}${NC}"
echo

# Confirm version
if [ "$1" != "--build-only" ]; then
    read -p "Is this version correct? (y/n): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        read -p "Enter new version: " VERSION
        # Update version in the Python file
        sed -i '' "s/APP_VERSION = \".*\"/APP_VERSION = \"$VERSION\"/" ttc_positions_app.py
        echo -e "${GREEN}Updated version to $VERSION${NC}"
    fi
fi

# Check for virtual environment
if [ ! -d "venv" ]; then
    echo -e "${RED}Error: Virtual environment not found. Run: python3 -m venv venv${NC}"
    exit 1
fi

# Activate virtual environment
echo -e "${YELLOW}Activating virtual environment...${NC}"
source venv/bin/activate

# Check for PyInstaller
if ! command -v pyinstaller &> /dev/null; then
    echo -e "${YELLOW}Installing PyInstaller...${NC}"
    pip install pyinstaller
fi

# Clean previous builds
echo -e "${YELLOW}Cleaning previous builds...${NC}"
rm -rf "$DIST_DIR" "$BUILD_DIR"

# Build the app
echo -e "${YELLOW}Building application...${NC}"
pyinstaller \
    --name "$APP_NAME" \
    --onefile \
    --windowed \
    --add-data "resources:resources" \
    --hidden-import="ib_async" \
    --hidden-import="webview" \
    --hidden-import="webview.platforms.cocoa" \
    ttc_positions_app.py

# Check if icon exists and add it
if [ -f "icon.icns" ]; then
    echo -e "${YELLOW}Adding icon to app bundle...${NC}"
    cp icon.icns "$DIST_DIR/$APP_NAME.app/Contents/Resources/"
fi

# Create resources folder alongside the app
echo -e "${YELLOW}Creating external resources folder...${NC}"
mkdir -p "$DIST_DIR/resources/templates"
mkdir -p "$DIST_DIR/resources/static/css"
mkdir -p "$DIST_DIR/resources/static/js"

# Copy resources if they exist
if [ -d "resources" ]; then
    cp -r resources/* "$DIST_DIR/resources/"
fi

# Create a DMG (disk image) for distribution
echo -e "${YELLOW}Creating DMG installer...${NC}"
DMG_NAME="TTC_Positions_Report_${VERSION}_Mac.dmg"

# Check if create-dmg is available
if command -v create-dmg &> /dev/null; then
    create-dmg \
        --volname "$APP_NAME" \
        --volicon "icon.icns" \
        --window-pos 200 120 \
        --window-size 600 400 \
        --icon-size 100 \
        --icon "$APP_NAME.app" 150 185 \
        --hide-extension "$APP_NAME.app" \
        --app-drop-link 450 185 \
        "$DIST_DIR/$DMG_NAME" \
        "$DIST_DIR/$APP_NAME.app"
else
    # Fallback: create simple DMG using hdiutil
    echo -e "${YELLOW}Note: Install create-dmg for prettier DMGs: brew install create-dmg${NC}"
    hdiutil create -volname "$APP_NAME" -srcfolder "$DIST_DIR/$APP_NAME.app" -ov -format UDZO "$DIST_DIR/$DMG_NAME"
fi

echo
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Build Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo
echo -e "  App: ${BLUE}$DIST_DIR/$APP_NAME.app${NC}"
echo -e "  DMG: ${BLUE}$DIST_DIR/$DMG_NAME${NC}"
echo

# If build-only flag, exit here
if [ "$1" == "--build-only" ]; then
    echo -e "${YELLOW}Build only mode - skipping release.${NC}"
    exit 0
fi

# Ask about GitHub release
echo -e "${YELLOW}Would you like to create a GitHub release?${NC}"
read -p "Create GitHub release? (y/n): " -n 1 -r
echo

if [[ $REPLY =~ ^[Yy]$ ]]; then
    # Check for GitHub CLI
    if ! command -v gh &> /dev/null; then
        echo -e "${YELLOW}GitHub CLI not found. Install with: brew install gh${NC}"
        echo -e "${YELLOW}Then run: gh auth login${NC}"
        echo
        echo -e "Manual release URL: https://github.com/YOUR_USERNAME/YOUR_REPO/releases/new"
        echo -e "Upload file: $DIST_DIR/$DMG_NAME"
        exit 0
    fi
    
    # Get release notes
    echo
    echo -e "${YELLOW}Enter release notes (press Ctrl+D when done):${NC}"
    RELEASE_NOTES=$(cat)
    
    # Create release
    echo -e "${YELLOW}Creating GitHub release v${VERSION}...${NC}"
    gh release create "v${VERSION}" \
        --title "v${VERSION}" \
        --notes "$RELEASE_NOTES" \
        "$DIST_DIR/$DMG_NAME"
    
    echo
    echo -e "${GREEN}Release v${VERSION} created successfully!${NC}"
    gh release view "v${VERSION}" --web
fi

echo
echo -e "${GREEN}Done!${NC}"

