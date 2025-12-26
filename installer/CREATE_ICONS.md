# Creating App Icons

The `icon.svg` file contains the app icon design. You need to convert it to platform-specific formats.

## Quick Option: Use an Online Converter

1. Go to https://cloudconvert.com/svg-to-ico or https://convertio.co/svg-ico/
2. Upload `icon.svg`
3. Download as `icon.ico` for Windows
4. Use https://cloudconvert.com/svg-to-icns for Mac

## Option 2: Use ImageMagick (if installed)

```bash
# For Windows (.ico) - creates multi-resolution icon
convert icon.svg -define icon:auto-resize=256,128,64,48,32,16 icon.ico

# For Mac (.icns) - need to create iconset first
mkdir icon.iconset
for size in 16 32 64 128 256 512; do
  convert icon.svg -resize ${size}x${size} icon.iconset/icon_${size}x${size}.png
  convert icon.svg -resize $((size*2))x$((size*2)) icon.iconset/icon_${size}x${size}@2x.png
done
iconutil -c icns icon.iconset -o icon.icns
```

## Option 3: Use macOS Preview (Mac only)

1. Open `icon.svg` in a browser
2. Take a screenshot of just the icon (Cmd+Shift+4)
3. Open in Preview
4. File > Export, choose ICNS format

## Required Files

After conversion, place these files in the project:

- `installer/icon.ico` - Windows icon (for installer and exe)
- `icon.icns` - Mac icon (for .app bundle)

## Placeholder Icon

If you don't have icons ready, the build will still work - it will just use default system icons.

