# TTC Positions Report

A desktop app that shows a real trader (Dad — a wheel-strategy options trader
running everything through IBKR/TWS) his positions, option tranches, and
premium income at a glance, with auto-updates so he never has to think about
installing a new version. Brandon builds and maintains it; Dad is the only
real end user. That single fact should drive every UI/UX decision: correctness
and glanceability for someone actively trading beat polish or cleverness every
time. If a feature could plausibly cause Dad to misread a number and place a
bad trade, that's a P0, not a nitpick.

Stack: Flask + waitress (backend), a native window via `pywebview` (frontend
host), vanilla JS/CSS with **no bundler, no framework, no build step** — every
file in `ui/static/js/` is a plain `<script>` tag loaded in order
(`app.js` → `tranches.js` → `income.js` → `settings.js`), executing in shared
global scope. Packaged with PyInstaller into a single Windows exe (and a Mac
`.app`/`.dmg` for Brandon's dev use only — Windows is the actual target).

## Architecture: three directories, on purpose

`ttc_app/config.py` defines three distinct directories and the boundary
between them is load-bearing, not incidental:

- **`APP_DIR`** — wherever the exe currently lives. In production this is a
  Dropbox-synced folder on Dad's PC (see "Deployment" below). Only small,
  conflict-tolerant files belong here: logs, `version.json`, legacy JSON
  mirrors. `APP_DIR = os.path.dirname(sys.executable)` when frozen — the
  self-updater overwrites the exe **in place** at whatever this path is
  (`app_update.py`), so shortcuts survive updates only if the filename never
  changes across releases (see below).
- **`DATA_DIR`** — `%LOCALAPPDATA%\TTC_Positions` (Windows) / Application
  Support (Mac). The SQLite DB (`ttc.db`) lives here and **must never** move
  into the Dropbox folder — Dropbox syncing a live SQLite WAL file causes
  corruption and conflicted copies. This bit us once; don't reintroduce it.
- **`UI_DIR`** — bundled `templates`/`static`, resolved via `sys._MEIPASS` when
  frozen. Static, ships with the exe, never written to at runtime.

## Deployment model (why the filename matters)

Two independent mechanisms both need to agree on **one stable filename**
(`TTC_Positions_Report_Windows.exe`, `STABLE_WINDOWS_ASSET` in
`app_update.py`) or shortcuts silently break:

1. **In-app auto-updater** (`app_update.py`): downloads the stable-named
   release asset, verifies its SHA-256 against `SHA256SUMS.txt`, and swaps it
   over `sys.executable` via a generated `.bat` helper (with retry-on-lock,
   since Dropbox briefly locks files during sync).
2. **`deploy_to_dad.sh`** (repo-root, gitignored — contains Brandon's local
   Dropbox path): a manual/bootstrap fallback that downloads the same
   stable-named asset and deploys it into Dad's Dropbox folder directly,
   archiving whatever was previously there (named by the version it actually
   was, read from `version.json`) into `old_versions/`.

If you ever reintroduce a *versioned* filename into either path (tempting for
"which version is this" clarity), you will break Dad's desktop shortcut on the
next release. Don't.

## Backend module map

| File | Responsibility |
|---|---|
| `main.py` | Startup sequence: logging, DB, IBKR manager, Flask server thread, native window. |
| `web.py` | All Flask routes. Largest file — this is where API response shapes live. |
| `ibkr_manager.py` | Persistent IBKR connection owned by one background thread + its own asyncio loop. Rewritten in v2.2.0 to fix ~70% handshake-timeout rates caused by the old per-request-connect pattern — do not go back to connect-per-request. Contract qualification for a large portfolio must stay batched (`qualifyContractsAsync(*contracts)` in one call), not looped one-at-a-time — that regression already caused a real production timeout once (cold-start `get_snapshot()` blowing past its 25s deadline). |
| `tranches.py` | Pure functional tranche/wheel-cycle reconstruction from trade history. **FIFO by design** — it reconstructs what IBKR actually reported (which lot really closed), it does not and should not get changed to prescribe LIFO or any other strategy. Any "which lot should Dad sell" logic is a presentation-layer signal computed elsewhere (`web.py`), never a change to matching order in here. |
| `db.py` | SQLite schema + accessors. Tranches/tranche_events are fully rebuilt from `trades` on every read (`replace_tranches`), never incrementally patched. |
| `flex_client.py` | IBKR Flex Web Service trade-history import (daily auto + manual). |
| `price_sources.py` | Yahoo → Cboe → cached-DB fallback chain when IBKR is unavailable. Stooq was tried and is dead (JS proof-of-work wall) — don't re-add it. Never strip exchange suffixes from symbols (e.g. `WBD.TEN` ≠ `WBD`). |
| `app_update.py` | Self-update: check → download → SHA-256 verify → in-place swap. Fails closed if a release has no `SHA256SUMS.txt`. |
| `config.py` | The three-directory split above; `APP_VERSION` is the **single** source of truth for version (UI, updater, and CI all read it — never hardcode a version string anywhere else). |

## Conventions worth following (learned the hard way this project)

**Single source of truth, always.** This codebase has twice organically grown
duplicate copies of the same list (three independent column-header arrays in
`app.js`; version strings that used to drift). When adding a column, a status
value, an enum — grep for every existing occurrence first and consolidate
into one definition before adding a new one, don't add a fourth copy.

**All client-only UI state lives in one localStorage blob.** `app.js` defines
`PREFS_KEY = "ttc_positions_prefs"` with `loadPreferences()`/
`savePreferences()` doing a shallow merge into one JSON blob. Dark mode,
compact view, refresh rate, collapsed sections, notification prefs, column
config — all of it. Never invent a second localStorage key, and never round-
trip UI-only state (like "is this section collapsed") through `/api/settings`
— that endpoint is reserved for real backend-persisted config (Flex Query
credentials, buyback threshold, premium goals) that has to be the same
regardless of which browser/session is looking at it.

**Anything that auto-refreshes must survive the refresh.** The positions
table polls on a timer; any interactive state a user sets (sort column, filter
text, column widths) needs to be explicitly re-applied after each periodic
rebuild, or it silently reverts and looks like a bug. This has bitten the app
before (sort order used to get wiped on every auto-refresh) — check this
whenever you touch anything that both users can adjust and that periodic
refresh rebuilds from scratch.

**Release process**: bump `APP_VERSION` in `config.py`, commit, `git tag
vX.Y.Z`, `git push origin main --tags` — the tag push triggers
`.github/workflows/build-release.yml` (test → build-windows → build-mac →
release). See README's "Creating Updates" section for the full walkthrough.
No branch protection on `main`; direct pushes for tagged releases are the
norm here, not an exception.

**Run the test suite before any commit** (`python -m pytest -q`, currently
fast — well under a second). For anything UI-visible, also actually drive it
in a browser (`run` or `verify` skill) — passing tests don't prove a chart
renders or a badge shows up in the right place.

## Off-Piste

When the documented APIs or official behavior don't get us where we need to
go, we go OFF-PISTE — reverse-engineer the actual wire format, dissect what
the real endpoint returns, hand-write the missing piece. Don't accept "the
library doesn't expose that" as a stopping point. Examples already in this
codebase:

- **Windows socket probing**: `connect_ex()` on a blocking socket with a
  timeout is genuinely ambiguous on Windows (WSAEWOULDBLOCK looks the same for
  "still connecting" and "connection refused"). `ibkr_manager.py`'s
  `probe_ib_ports()` uses a non-blocking connect + `select()` and reads the
  real outcome via `SO_ERROR` — because the "obvious" approach silently lied
  about whether TWS was actually listening.
- **Cboe delayed quotes**: no official free/keyless quote API exists for the
  fallback chain, so `price_sources.py` calls Cboe's own internal JSON
  endpoint (`cdn.cboe.com/api/global/delayed_quotes/quotes/{SYM}.json`) —
  the same one their own site's JS calls — rather than paying for a data feed.
- **Windows `.ico` packing without ImageMagick/Pillow**: when neither was
  available, we hand-wrote the ICO container format directly (`ICONDIR` +
  `ICONDIRENTRY` headers wrapping plain PNG bytes per size — valid since
  Vista) rather than pulling in a new dependency for one file.
- **IBKR Flex Query XML quirks**: `flex_client.py` and the trade-code parsing
  in `tranches.py` (`A` = assigned, `Ep` = expired, on the `codes` field) exist
  because IBKR's own docs don't fully specify how to distinguish assignment
  vs. expiration vs. buyback from the Flex export — that was reverse-engineered
  from real trade data.

The corollary: when something off-piste works, leave a comment explaining
*why* the weird approach is necessary (not what it does) so the next session
doesn't "simplify" it back into the broken obvious version.

## Boil the Ocean

The marginal cost of completeness is near zero with AI help. Do the whole
thing, not a placeholder for it. Do it with tests where tests make sense, and
with an actual browser check where they don't. Never offer to "table this for
later" when the real fix is reachable in the same session. Never leave a
dangling thread when tying it off takes five more minutes. The standard is
"this is actually done and Dad can use it today," not "here's a plan to build
it" — when asked for something, build the finished thing, not a proposal.
That said: for genuinely subjective calls (visual design direction, a tradeoff
only Brandon can weigh) ask rather than silently guess — boiling the ocean
means finishing the *work*, not skipping the decisions that are actually his
to make.
