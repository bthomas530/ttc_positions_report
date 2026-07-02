# TTC Positions Report - Desktop Application
# Thin entry point; the application lives in the ttc_app package.
# (This filename is the PyInstaller entry in CI and the local spec.)

from ttc_app.main import main

if __name__ == '__main__':
    main()
