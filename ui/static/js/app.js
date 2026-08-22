
// TTC Positions Report - Frontend JavaScript (core + Positions tab)

let isRefreshing = false;
// Sort state is per-section (positions/incomplete/watchlist each sort
// independently) and keyed by column key, not raw index, since hide/reorder
// changes which index a column renders at between refreshes.
let currentSort = { positions: null, incomplete: null, watchlist: null };
let refreshInterval;
let cachedData = null;
let optionsBySymbol = {};
let marketStatusInterval;
let lastConnectionSource = null;
const PREFS_KEY = "ttc_positions_prefs";
const loadedTabs = { positions: true };

const DEFAULT_NOTIFICATION_PREFS = {
    enabled: true,
    position: "bottom-right",
    categories: { refresh: false, dataSource: true, actions: true, errors: true },
};

// Single source of truth for columns across the three Positions-tab tables,
// the CSV exporter, and column customization prefs. `rowIndex` maps a column
// key to its position in the flat per-row array the backend sends for that
// section (see ttc_app/web.py enhance_with_market_data()); a key only lists
// the sections it actually appears in.
const DEFAULT_COLUMN_WIDTH = 120;
// Per-column defaults so headers like "Daily Change %" don't truncate at a
// flat width; Underlying gets extra room per the "a little wider" ask.
const DEFAULT_COLUMN_WIDTHS = {
    underlying: 190, shares: 90, current_price: 140, avg_price: 110,
    daily_change_dollar: 140, daily_change_pct: 130, last_price: 110,
    open: 100, ogap: 100, np: 70, cc: 70, uc: 70, shares_available: 140,
    data_source: 130,
};
function defaultWidthFor(key) {
    return DEFAULT_COLUMN_WIDTHS[key] || DEFAULT_COLUMN_WIDTH;
}

const COLUMN_DEFS = [
    { key: "underlying", label: "Underlying", pinned: true,
      rowIndex: { positions: 0, incomplete: 0, watchlist: 0 } },
    { key: "shares", label: "Shares",
      rowIndex: { positions: 1, incomplete: 1 } },
    { key: "current_price", label: "Current Price",
      rowIndex: { positions: 2, incomplete: 2, watchlist: 1 } },
    { key: "avg_price", label: "Avg Price",
      rowIndex: { positions: 3, incomplete: 3 } },
    { key: "daily_change_dollar", label: "Daily Change $",
      rowIndex: { positions: 4, incomplete: 4, watchlist: 2 } },
    { key: "daily_change_pct", label: "Daily Change %",
      rowIndex: { positions: 5, incomplete: 5, watchlist: 3 } },
    { key: "last_price", label: "Prev Close",
      rowIndex: { positions: 6, incomplete: 6, watchlist: 4 } },
    { key: "open", label: "Open",
      rowIndex: { positions: 7, incomplete: 7, watchlist: 5 } },
    { key: "ogap", label: "OGap",
      rowIndex: { positions: 8, incomplete: 8, watchlist: 6 } },
    { key: "np", label: "NP",
      rowIndex: { positions: 9 } },
    { key: "cc", label: "CC",
      rowIndex: { positions: 10 } },
    { key: "uc", label: "UC",
      rowIndex: { positions: 11 } },
    { key: "shares_available", label: "Shares Available",
      rowIndex: { positions: 12 } },
    // No natural row index -- rendered from getSourceInfo() instead.
    { key: "data_source", label: "Data Source", rowIndex: {} },
];
const COLUMN_DEFS_BY_KEY = Object.fromEntries(COLUMN_DEFS.map(c => [c.key, c]));

function columnKeysForSection(section) {
    return COLUMN_DEFS
        .filter(c => c.key === "data_source" || section in c.rowIndex)
        .map(c => c.key);
}

function rowValue(row, key, section) {
    const def = COLUMN_DEFS_BY_KEY[key];
    if (!def || !(section in def.rowIndex)) return undefined;
    return row[def.rowIndex[section]];
}

const DEFAULT_COLUMN_CONFIG = {
    positions: { hidden: ["data_source"] },
    incomplete: { hidden: ["data_source"] },
    watchlist: { hidden: ["data_source"] },
};

// Column config (order/hidden/widths per section) follows the same
// merge-over-defaults pattern as getNotificationPrefs(), stored in the same
// PREFS_KEY blob. `order` always lists every applicable non-underlying key
// (visible or not) so the Columns modal has something to render a row for;
// `hidden` marks which of those are currently off.
function getColumnConfig(section) {
    const applicable = columnKeysForSection(section);
    const applicableSet = new Set(applicable);
    const saved = (loadPreferences().columns || {})[section] || {};
    const defaults = DEFAULT_COLUMN_CONFIG[section];

    let order = (saved.order || []).filter(k => applicableSet.has(k) && k !== "underlying");
    applicable.forEach(k => { if (k !== "underlying" && !order.includes(k)) order.push(k); });

    const hidden = (saved.hidden !== undefined ? saved.hidden : defaults.hidden)
        .filter(k => applicableSet.has(k));
    const widths = { ...(saved.widths || {}) };

    return { order, hidden, widths };
}

function saveColumnConfig(section, partial) {
    const current = getColumnConfig(section);
    const merged = { ...current, ...partial };
    const allColumns = loadPreferences().columns || {};
    savePreferences({ columns: { ...allColumns, [section]: merged } });
}

function resetColumnConfig(section) {
    const allColumns = loadPreferences().columns || {};
    delete allColumns[section];
    savePreferences({ columns: allColumns });
}

function visibleColumnsForSection(section, config) {
    config = config || getColumnConfig(section);
    const hiddenSet = new Set(config.hidden);
    const orderedKeys = ["underlying", ...config.order.filter(k => !hiddenSet.has(k))];
    return orderedKeys.map(k => COLUMN_DEFS_BY_KEY[k]).filter(Boolean);
}

// ---------------------------------------------------------------------------
// Column help
// ---------------------------------------------------------------------------
// One registry for every table header in the app -- Positions, the option
// sub-table, Tranches, Income -- so a column's explanation lives in exactly
// one place no matter which file renders the header. Positions columns reuse
// their COLUMN_DEFS key verbatim; every other table namespaces its keys
// ("opt.", "tranche.", "income.") because the same word means different
// things in different tabs (Tranches "Premium" is per-lot, Income "Premium"
// is per-period).
//
// Each entry: { what } is plain English, { calc } is the actual arithmetic,
// { note } is anything else worth knowing. `calc` may be a function when the
// text depends on live data (e.g. the configured buyback threshold).
const COLUMN_HELP = {
    // ---- Positions / Incomplete Lots / Watchlist ----
    underlying: {
        what: "The stock this row is about. Click the ticker to open it on TradingView.",
        note: "A number badge means this symbol has open option contracts — click the row to expand them.",
    },
    shares: {
        what: "Shares of the stock currently held. Negative means a short stock position.",
        calc: "IBKR's position quantity, as-is. 0 shares with an option badge means options only — a cash-secured put with no stock behind it yet.",
    },
    current_price: {
        what: "What the stock is trading at right now.",
        calc: "IBKR's market price (midpoint of bid/ask during the session, last trade otherwise). If IBKR can't price it: Yahoo → Cboe delayed → last price saved in the database.",
        note: "A colored dot next to the price means it did NOT come from IBKR — hover the dot for the source and its age.",
    },
    avg_price: {
        what: "Average cost per share for the whole stock position.",
        calc: "IBKR's average cost, including commissions. This is IBKR's blended number across every lot — the Tranches tab breaks the same shares into individual lots and applies option premium to each.",
    },
    daily_change_dollar: {
        what: "How far the stock has moved per share today.",
        calc: "Current Price − Prev Close. Green above zero, red below.",
    },
    daily_change_pct: {
        what: "Today's move as a percentage of yesterday's close.",
        calc: "(Current Price − Prev Close) ÷ Prev Close.",
    },
    last_price: {
        what: "The previous session's closing price — yesterday's close, not the latest trade. Today's price is Current Price.",
        calc: "IBKR's close for the symbol. Every daily-change number on this row is measured against it.",
    },
    open: {
        what: "Where the stock opened this session.",
        calc: "IBKR's open for the current session.",
    },
    ogap: {
        what: "The overnight gap — how far the stock opened away from where it closed the day before.",
        calc: "Open − Prev Close. Positive gapped up overnight, negative gapped down.",
    },
    np: {
        what: "Put contracts written on this symbol — the cash-secured puts.",
        calc: "Absolute value of the net put position (all put contract quantities for the symbol, summed). 1 contract = an obligation to buy 100 shares at the strike if assigned.",
    },
    cc: {
        what: "Written calls that are covered by shares already held.",
        calc: "The smaller of: whole 100-share lots held, or total call contracts. Only counted when the stock position is long.",
    },
    uc: {
        what: "Written calls with no shares behind them — assignment would mean buying shares at market or going short.",
        calc: "Total call contracts − whole 100-share lots held, floored at 0. With no long stock, every call counts as uncovered.",
    },
    shares_available: {
        what: "Shares not already pledged against written calls — what could be sold without leaving a call uncovered.",
        calc: "Shares − (CC × 100) − (UC × 100). A negative number means calls are written against more shares than are actually held.",
    },
    data_source: {
        what: "Where this row's price came from on the last refresh.",
        calc: "Fallback order: IBKR (live) → Yahoo Finance → Cboe (delayed) → Cached (the last price saved in the database, shown with its age).",
    },

    // ---- Option sub-table (expand a Positions row) ----
    "opt.contract": {
        what: "Whether this contract is a put or a call.",
    },
    "opt.pos": {
        what: "How many contracts are held. Negative means sold/written — the normal case on the wheel.",
        calc: "IBKR's position quantity for the contract. 1 contract = 100 shares.",
    },
    "opt.strike": {
        what: "The contract's strike — the price shares change hands at if it's assigned.",
    },
    "opt.expiry": {
        what: "The date the contract expires.",
    },
    "opt.dte": {
        what: "Days to expiration.",
        calc: "Calendar days from today to the expiration date. 0d expires today; a negative number means it's past expiry and hasn't cleared out of the account yet.",
    },
    "opt.delta": {
        what: "Roughly how much the contract's price moves for a $1 move in the stock — and a rough stand-in for the odds of finishing in the money (0.30 ≈ 30%).",
        calc: "IBKR's model greek. Blank when IBKR hasn't sent greeks for the contract yet.",
    },
    "opt.theta": {
        what: "Time decay — about what the contract loses in value per day, all else equal. On a written option that decay is working for you.",
        calc: "IBKR's model greek, per share. Multiply by 100 for one contract.",
    },
    "opt.iv": {
        what: "Implied volatility — how much movement the option's price implies, annualized. Higher IV means richer premium.",
        calc: "IBKR's model implied volatility, shown as a percentage.",
    },
    "opt.entry": {
        what: "The price per share the contract was opened at — on a written option, the premium collected.",
        calc: "IBKR's average cost for the position ÷ the multiplier (100). Multiply by 100 for dollars per contract.",
    },
    "opt.mark": {
        what: "What the contract is worth right now, per share. On a written option that's roughly what it would cost to buy it back and close the trade.",
        calc: "IBKR's market price for the contract — the midpoint of bid/ask during the session, falling back to the last trade, then the previous close. Multiply by 100 for dollars to close one contract.",
        note: "Entry is what came in when it was sold; Mark is what it would cost to get out today. The gap between them is the profit so far.",
    },
    "opt.prem_left": {
        what: "How much of the premium is still at risk on a written option. 20% left means 80% of the premium has already been earned.",
        calc: () => "Mark ÷ Entry × 100. The row turns amber with a BUYBACK TARGET badge at or below "
            + (cachedData && cachedData.buyback_threshold_pct ? cachedData.buyback_threshold_pct + "%" : "the threshold")
            + ", set under Settings → Trading Preferences.",
    },

    // ---- Tranches tab: open lots ----
    "tranche.opened": {
        what: "The date this lot of shares was acquired, plus badges for how it opened and how long it's been held.",
        calc: "Taken from the imported trade history — either a stock buy or a put assignment (PUT ASSIGNED).",
        note: "SEEDED means the lot predates the imported trade history, so its real open date is unknown and no long/short-term badge is shown.",
    },
    "tranche.qty": {
        what: "Shares in this lot.",
        calc: "Lots are rebuilt FIFO from the trade history, which is the order IBKR actually closed them in. A lot is split only when a covered call needs exactly 100 shares.",
    },
    "tranche.open_price": {
        what: "Price per share paid to open the lot.",
        calc: "The trade price on a buy, or the put's strike on an assignment. Commissions are carried in Premium, not here.",
    },
    "tranche.premium": {
        what: "Net option premium collected against these specific shares.",
        calc: "The assigned put's premium carried into the lot, plus covered-call premium (one contract per 100 shares), minus anything paid to buy those options back. All net of commissions.",
    },
    "tranche.net_basis": {
        what: "The effective cost per share once premium is applied — the real break-even on these shares.",
        calc: "Open Price − (Premium ÷ Qty).",
    },
    "tranche.current": {
        what: "The latest price per share for the symbol.",
        calc: "Live IBKR price when connected, otherwise the most recent price saved in the database.",
    },
    "tranche.unrealized": {
        what: "Where this lot stands right now, premium included.",
        calc: "(Current − Open Price) × Qty + Premium. Blank when no current price is available.",
    },
    "tranche.covering_call": {
        what: "The written call currently covering these shares, if there is one.",
        calc: "Shows the covering call's strike and expiry. When it's there, selling these shares would leave that call uncovered — hence the warning.",
    },

    // ---- Tranches tab: closed lots ----
    "tranche.closed": {
        what: "The date the lot was closed out.",
    },
    "tranche.close_price": {
        what: "Price per share the lot went out at.",
        calc: "The sale price, or the call's strike when the shares were called away.",
    },
    "tranche.how": {
        what: "How the lot closed — sold on the open market, or CALLED AWAY by an assigned covered call.",
    },
    "tranche.realized": {
        what: "The final profit or loss on this lot, premium included.",
        calc: "Cash received at close − cash paid at open + all premium attributed to the lot, every piece net of commissions.",
    },

    // ---- Income tab ----
    "income.symbol": {
        what: "The underlying the premium came from.",
        calc: "Every put and call premium event for that symbol, summed across the whole imported history.",
    },
    "income.premium_symbol": {
        what: "Net option premium collected on this symbol.",
        calc: "Credits from selling puts and calls, minus what was paid to buy them back, net of commissions.",
    },
    "income.week": {
        what: "An ISO week, labeled year-Wnn (weeks run Monday through Sunday).",
    },
    "income.month": {
        what: "A calendar month, labeled year-mm.",
    },
    "income.premium_period": {
        what: "Net option premium collected in this period.",
        calc: "Credits from selling puts and calls, minus buybacks, net of commissions — bucketed by each trade's own date.",
        note: "Assignments are not counted again here; that premium was already booked on the day the option was sold.",
    },
    "income.assign_date": {
        what: "When the assignment happened.",
    },
    "income.assign_symbol": {
        what: "The underlying whose shares moved.",
    },
    "income.assign_what": {
        what: "Which direction the shares went: a written put was assigned and shares came in, or a written call was assigned and shares were called away.",
    },
    "income.assign_amount": {
        what: "The cash tied to that assignment event.",
        calc: "On a put assignment: the put premium carried into the new lot of shares. On a called-away lot: the proceeds from the shares going out, net of commissions.",
    },
    "income.card_week": {
        what: "Net option premium collected so far in the current ISO week (Monday through Sunday).",
        calc: "Same arithmetic as the Premium by week table — sales minus buybacks, net of commissions.",
    },
    "income.card_month": {
        what: "Net option premium collected so far in the current calendar month.",
        calc: "Same arithmetic as the Premium by month table — sales minus buybacks, net of commissions.",
    },
    "income.card_realized": {
        what: "Profit and loss already banked from lots that are completely closed out.",
        calc: "Sum of Realized P/L across every closed tranche, which already includes the premium attributed to those lots.",
    },
    "income.card_unrealized": {
        what: "Where all the still-open lots stand right now, premium included.",
        calc: "Summed over open tranches: (Current − Open Price) × Qty + Premium. Shows a dash when no current prices are available.",
    },
    "income.outcome_expired": {
        what: "Written options that ran to expiration worthless — the wheel working exactly as intended.",
        calc: "Count of expiry events, and the premium those contracts brought in.",
    },
    "income.outcome_bought_back": {
        what: "Written options closed early by buying them back.",
        calc: "Count of buyback trades, and their net cash effect (a cost, so normally negative).",
    },
    "income.outcome_assigned": {
        what: "Written puts that were assigned, putting shares into the account.",
        calc: "Count of put assignments, and the premium carried into the resulting lots.",
    },
};

// A header label wrapped so it can carry a hover explanation. Used by the
// string-built tables (option sub-table, Tranches, Income); createTable()
// builds the equivalent node directly.
function helpLabel(label, key) {
    return '<span class="th-help" data-help="' + escapeHtml(key) + '">' + escapeHtml(label) + '</span>';
}

// <th> with a help-enabled label, for the string-built tables.
function helpTh(label, key, extraAttrs) {
    return '<th' + (extraAttrs ? ' ' + extraAttrs : '') + '>' + helpLabel(label, key) + '</th>';
}

let helpTipEl = null;

// The tooltip lives on <body>, not inside the <th>. Every table here sits in
// a .table-container with overflow-x:auto, which would clip a tooltip
// rendered as a descendant of the cell.
function ensureHelpTip() {
    if (!helpTipEl) {
        helpTipEl = document.createElement("div");
        helpTipEl.id = "help-tooltip";
        helpTipEl.setAttribute("role", "tooltip");
        document.body.appendChild(helpTipEl);
    }
    return helpTipEl;
}

function showHelpTip(target) {
    const entry = COLUMN_HELP[target.dataset.help];
    if (!entry) return;
    const tip = ensureHelpTip();
    const calc = typeof entry.calc === "function" ? entry.calc() : entry.calc;
    const title = target.dataset.helpTitle || target.textContent.trim();
    let html = '<div class="help-tip-title">' + escapeHtml(title) + '</div>';
    html += '<div class="help-tip-what">' + escapeHtml(entry.what) + '</div>';
    if (calc) {
        html += '<div class="help-tip-calc"><span class="help-tip-label">How it’s calculated</span>'
            + escapeHtml(calc) + '</div>';
    }
    if (entry.note) {
        html += '<div class="help-tip-note">' + escapeHtml(entry.note) + '</div>';
    }
    tip.innerHTML = html;
    tip.classList.add("visible");
    positionHelpTip(target, tip);
}

function positionHelpTip(target, tip) {
    const rect = target.getBoundingClientRect();
    const tipRect = tip.getBoundingClientRect();
    const margin = 10;

    let top = rect.bottom + 8;
    // Headers are sticky at the top of a scrolling table, so below is almost
    // always right; flip above only when the bottom of the window is closer.
    if (top + tipRect.height > window.innerHeight - margin && rect.top > tipRect.height + margin) {
        top = rect.top - tipRect.height - 8;
    }
    let left = rect.left + rect.width / 2 - tipRect.width / 2;
    left = Math.max(margin, Math.min(left, window.innerWidth - tipRect.width - margin));

    tip.style.top = Math.round(top) + "px";
    tip.style.left = Math.round(left) + "px";
}

function hideHelpTip() {
    if (helpTipEl) helpTipEl.classList.remove("visible");
}

// Delegated so it keeps working across every table rebuild -- the Positions
// tables are torn down and recreated on each auto-refresh, and the Tranches
// and Income tabs re-render wholesale.
document.addEventListener("mouseover", (e) => {
    const target = e.target.closest("[data-help]");
    if (target) showHelpTip(target);
});
document.addEventListener("mouseout", (e) => {
    if (e.target.closest("[data-help]")) hideHelpTip();
});
document.addEventListener("focusin", (e) => {
    const target = e.target.closest("[data-help]");
    if (target) showHelpTip(target);
});
document.addEventListener("focusout", (e) => {
    if (e.target.closest("[data-help]")) hideHelpTip();
});
window.addEventListener("scroll", hideHelpTip, true);

// Debug logging
function log(msg) {
    console.log("[TTC] " + msg);
}

function loadPreferences() {
    try {
        return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
    } catch (e) {
        return {};
    }
}

function savePreferences(prefs) {
    try {
        localStorage.setItem(PREFS_KEY, JSON.stringify({ ...loadPreferences(), ...prefs }));
    } catch (e) {
        log("Failed to save preferences: " + e);
    }
}

function getNotificationPrefs() {
    const saved = loadPreferences().notifications || {};
    return {
        ...DEFAULT_NOTIFICATION_PREFS,
        ...saved,
        categories: { ...DEFAULT_NOTIFICATION_PREFS.categories, ...(saved.categories || {}) },
    };
}

function saveNotificationPrefs(partial) {
    const current = getNotificationPrefs();
    savePreferences({
        notifications: {
            ...current,
            ...partial,
            categories: { ...current.categories, ...(partial.categories || {}) },
        },
    });
}

function applyToastPosition(notif) {
    const container = document.getElementById("toast-container");
    container.classList.toggle("position-top-right", notif.position !== "bottom-right");
    container.classList.toggle("position-bottom-right", notif.position === "bottom-right");
}

function applyPreferences() {
    const prefs = loadPreferences();
    if (prefs.darkMode) {
        document.documentElement.setAttribute("data-theme", "dark");
        document.getElementById("darkModeToggle").innerHTML = '<i class="fas fa-sun"></i>';
    }
    if (prefs.compactView) {
        document.body.classList.add("compact");
    }
    if (prefs.refreshRate !== undefined) {
        document.getElementById("refreshRate").value = prefs.refreshRate;
    }
    if (prefs.collapsedSections) {
        prefs.collapsedSections.forEach(section => {
            const el = document.getElementById(section + "-section");
            if (el) el.classList.add("collapsed");
        });
    }
    applyToastPosition(getNotificationPrefs());
}

// category: "refresh" (routine auto-refresh pings), "dataSource" (IBKR/fallback
// transitions), "actions" (explicit user-initiated feedback), "errors"
function showToast(message, type = "info", duration = 3000, category = "actions") {
    const notif = getNotificationPrefs();
    if (!notif.enabled || notif.categories[category] === false) return;

    const container = document.getElementById("toast-container");
    const toast = document.createElement("div");
    toast.className = "toast " + type;
    const icons = { success: "fa-check-circle", error: "fa-exclamation-circle", info: "fa-info-circle" };
    toast.innerHTML = '<i class="fas ' + icons[type] + ' toast-icon"></i><span class="toast-message">' + message + '</span><button class="toast-close" onclick="this.parentElement.remove()"><i class="fas fa-times"></i></button>';
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.animation = "slideOut 0.3s ease forwards";
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

function updateMarketStatus() {
    const now = new Date();
    const eastern = new Date(now.toLocaleString("en-US", { timeZone: "America/New_York" }));
    const day = eastern.getDay();
    const hours = eastern.getHours();
    const minutes = eastern.getMinutes();
    const totalMinutes = hours * 60 + minutes;
    const marketOpen = 570; // 9:30 AM
    const marketClose = 960; // 4:00 PM
    
    const statusEl = document.getElementById("marketStatus");
    const countdownEl = document.getElementById("marketCountdown");
    const textEl = statusEl.querySelector(".status-text");
    
    const isWeekend = day === 0 || day === 6;
    const isOpen = !isWeekend && totalMinutes >= marketOpen && totalMinutes < marketClose;
    
    statusEl.classList.remove("open", "closed");
    statusEl.classList.add(isOpen ? "open" : "closed");
    
    if (isOpen) {
        textEl.textContent = "Market Open";
        const remaining = marketClose - totalMinutes;
        countdownEl.textContent = "Closes in " + Math.floor(remaining / 60) + "h " + (remaining % 60) + "m";
    } else {
        textEl.textContent = "Market Closed";
        let minutesUntilOpen;
        if (isWeekend) {
            minutesUntilOpen = (day === 0 ? 1 : 2) * 24 * 60 + marketOpen - totalMinutes;
        } else if (totalMinutes < marketOpen) {
            minutesUntilOpen = marketOpen - totalMinutes;
        } else {
            minutesUntilOpen = 1440 - totalMinutes + marketOpen;
        }
        const hoursUntil = Math.floor(minutesUntilOpen / 60);
        countdownEl.textContent = hoursUntil > 24 
            ? "Opens in " + Math.floor(hoursUntil / 24) + "d " + (hoursUntil % 24) + "h"
            : "Opens in " + hoursUntil + "h " + (minutesUntilOpen % 60) + "m";
    }
}

function toggleDarkMode() {
    const html = document.documentElement;
    const isDark = html.getAttribute("data-theme") === "dark";
    html.setAttribute("data-theme", isDark ? "light" : "dark");
    document.getElementById("darkModeToggle").innerHTML = isDark ? '<i class="fas fa-moon"></i>' : '<i class="fas fa-sun"></i>';
    savePreferences({ darkMode: !isDark });
    showToast((isDark ? "Light" : "Dark") + " mode enabled", "info", 1500);
}

function toggleCompactView() {
    const isCompact = document.body.classList.toggle("compact");
    savePreferences({ compactView: isCompact });
    showToast((isCompact ? "Compact" : "Normal") + " view enabled", "info", 1500);
}

function toggleSection(section) {
    const el = document.getElementById(section + "-section");
    el.classList.toggle("collapsed");
    const prefs = loadPreferences();
    const collapsed = prefs.collapsedSections || [];
    if (el.classList.contains("collapsed")) {
        if (!collapsed.includes(section)) collapsed.push(section);
    } else {
        const idx = collapsed.indexOf(section);
        if (idx > -1) collapsed.splice(idx, 1);
    }
    savePreferences({ collapsedSections: collapsed });
}

function openShortcutsModal() {
    document.getElementById("shortcuts-modal").classList.add("active");
}

function closeShortcutsModal() {
    document.getElementById("shortcuts-modal").classList.remove("active");
}

function openDiagnosticsModal() {
    document.getElementById("diagnostics-modal").classList.add("active");
    loadDiagnostics();
}

function closeDiagnosticsModal() {
    document.getElementById("diagnostics-modal").classList.remove("active");
}

let columnsModalSection = null;

function openColumnsModal(section) {
    columnsModalSection = section;
    renderColumnsModalBody(section);
    document.getElementById("columns-modal").classList.add("active");
}

function closeColumnsModal() {
    document.getElementById("columns-modal").classList.remove("active");
    columnsModalSection = null;
    // Reflect any hide/show/reorder/width changes immediately rather than
    // waiting for the next auto-refresh tick.
    if (cachedData) updateTables();
}

function renderColumnsModalBody(section) {
    const config = getColumnConfig(section);
    const rows = ["underlying", ...config.order];

    const body = document.getElementById("columns-body");
    body.innerHTML = "";
    rows.forEach((key, idx) => {
        const def = COLUMN_DEFS_BY_KEY[key];
        if (!def) return;
        const pinned = key === "underlying";
        const rowEl = document.createElement("div");
        rowEl.className = "columns-row" + (pinned ? " pinned" : "");

        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = !config.hidden.includes(key);
        checkbox.disabled = pinned;
        checkbox.addEventListener("change", () => {
            const hidden = config.hidden.filter(k => k !== key);
            if (!checkbox.checked) hidden.push(key);
            saveColumnConfig(section, { hidden });
            renderColumnsModalBody(section);
        });

        const name = document.createElement("span");
        name.className = "col-name th-help";
        name.dataset.help = def.key;
        name.dataset.helpTitle = def.label;
        name.textContent = def.label + (pinned ? " (always shown)" : "");

        const width = document.createElement("input");
        width.type = "number";
        width.className = "col-width";
        width.min = 50;
        width.step = 10;
        width.title = "Width (px)";
        width.value = config.widths[key] || defaultWidthFor(key);
        width.addEventListener("change", () => {
            const val = Math.max(50, parseInt(width.value) || defaultWidthFor(key));
            const current = getColumnConfig(section);
            saveColumnConfig(section, { widths: { ...current.widths, [key]: val } });
        });

        rowEl.appendChild(checkbox);
        rowEl.appendChild(name);
        if (!pinned) {
            const reorder = document.createElement("div");
            reorder.className = "col-reorder";
            const upBtn = document.createElement("button");
            upBtn.type = "button";
            upBtn.innerHTML = '<i class="fas fa-arrow-up"></i>';
            upBtn.disabled = idx <= 1;
            upBtn.addEventListener("click", () => moveColumn(section, key, -1));
            const downBtn = document.createElement("button");
            downBtn.type = "button";
            downBtn.innerHTML = '<i class="fas fa-arrow-down"></i>';
            downBtn.disabled = idx === rows.length - 1;
            downBtn.addEventListener("click", () => moveColumn(section, key, 1));
            reorder.appendChild(upBtn);
            reorder.appendChild(downBtn);
            rowEl.appendChild(reorder);
        }
        rowEl.appendChild(width);
        body.appendChild(rowEl);
    });
}

function moveColumn(section, key, delta) {
    const config = getColumnConfig(section);
    const order = config.order.slice();
    const idx = order.indexOf(key);
    const newIdx = idx + delta;
    if (idx === -1 || newIdx < 0 || newIdx >= order.length) return;
    [order[idx], order[newIdx]] = [order[newIdx], order[idx]];
    saveColumnConfig(section, { order });
    renderColumnsModalBody(section);
}

function resetColumnsToDefault() {
    if (!columnsModalSection) return;
    resetColumnConfig(columnsModalSection);
    renderColumnsModalBody(columnsModalSection);
}

function escapeHtml(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[c]);
}

async function loadDiagnostics() {
    const body = document.getElementById("diagnostics-body");
    body.innerHTML = '<div class="diagnostics-loading"><i class="fas fa-circle-notch fa-spin"></i> Running probe...</div>';
    try {
        const response = await fetch("/api/diagnostics");
        const data = await response.json();
        renderDiagnostics(data);
    } catch (err) {
        body.innerHTML = '<div class="diagnostics-loading">Could not load diagnostics: ' + escapeHtml(err.message) + '</div>';
    }
}

function renderDiagnostics(data) {
    const body = document.getElementById("diagnostics-body");
    const verdict = data.verdict || "unknown";
    const userMsg = data.user_message || "";
    const breaker = data.breaker || {};
    const cache = data.cache || {};
    const lastSuccess = data.last_success || {};
    const endpoints = data.endpoints || [];

    const endpointRows = endpoints.map(ep => {
        const status = ep.reachable
            ? '<span class="diag-status-pill up">OPEN</span>'
            : '<span class="diag-status-pill down">CLOSED</span>';
        const err = ep.error ? escapeHtml(ep.error) : "";
        return '<tr>' +
            '<td>' + escapeHtml(ep.label) + '</td>' +
            '<td>' + escapeHtml(ep.host) + ':' + escapeHtml(ep.port) + '</td>' +
            '<td>' + status + '</td>' +
            '<td>' + escapeHtml(ep.latency_ms) + ' ms</td>' +
            '<td>' + err + '</td>' +
            '</tr>';
    }).join("");

    const breakerLine = breaker.open
        ? 'OPEN — retry in ' + (breaker.retry_in_seconds || 0) + 's (' + (breaker.consecutive_failures || 0) + ' consecutive failures)'
        : 'closed (' + (breaker.consecutive_failures || 0) + ' recent failures)';

    const lastSuccessLine = lastSuccess.timestamp
        ? (lastSuccess.label || "?") + ' — ' + (lastSuccess.age || "")
        : 'never this session';

    body.innerHTML =
        '<div class="diag-section">' +
            '<div class="diag-verdict ' + escapeHtml(verdict) + '">' +
                '<span class="diag-verdict-label">' + escapeHtml(verdict.replace(/_/g, " ").toUpperCase()) + '</span>' +
                escapeHtml(userMsg) +
            '</div>' +
        '</div>' +
        '<div class="diag-section">' +
            '<h4>IBKR Endpoints</h4>' +
            '<table class="diag-table">' +
                '<thead><tr><th>Label</th><th>Address</th><th>Status</th><th>Latency</th><th>Error</th></tr></thead>' +
                '<tbody>' + endpointRows + '</tbody>' +
            '</table>' +
        '</div>' +
        '<div class="diag-section">' +
            '<h4>State</h4>' +
            '<div class="diag-meta-grid">' +
                '<div><div class="diag-meta-label">Circuit Breaker</div><div class="diag-meta-value">' + escapeHtml(breakerLine) + '</div></div>' +
                '<div><div class="diag-meta-label">Last Successful Connect</div><div class="diag-meta-value">' + escapeHtml(lastSuccessLine) + '</div></div>' +
                '<div><div class="diag-meta-label">Cache</div><div class="diag-meta-value">' + escapeHtml(cache.symbols || 0) + ' symbols, ' + escapeHtml(cache.age || "n/a") + '</div></div>' +
                '<div><div class="diag-meta-label">Client ID</div><div class="diag-meta-value">' + escapeHtml(data.client_id) + '</div></div>' +
                '<div style="grid-column:1/-1"><div class="diag-meta-label">Platform</div><div class="diag-meta-value">' + escapeHtml(data.platform) + ' — App v' + escapeHtml(data.app_version) + '</div></div>' +
            '</div>' +
        '</div>';
}

function exportToCSV() {
    if (!cachedData) {
        showToast("No data to export", "error", 3000, "errors");
        return;
    }
    // Exports whatever columns are currently visible/ordered on the
    // Positions table, so hiding/reordering columns there also reshapes CSVs.
    const cols = visibleColumnsForSection("positions");
    let csv = cols.map(c => c.label).join(",") + "\n";
    cachedData.positions.forEach(row => {
        const { source } = getSourceInfo(row, "positions");
        csv += cols.map(c => {
            if (c.key === "data_source") return SOURCE_LABELS[source] || source;
            const val = row[c.rowIndex.positions];
            if (c.key === "daily_change_pct" && typeof val === "number") return (val * 100).toFixed(2) + "%";
            return typeof val === "number" ? val.toFixed(2) : val;
        }).join(",") + "\n";
    });
    const blob = new Blob([csv], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "ttc_positions_" + new Date().toISOString().split("T")[0] + ".csv";
    a.click();
    showToast("Exported to CSV", "success");
}

function formatNumber(value, key) {
    if (value === "" || value === null || value === undefined) return "";
    const num = parseFloat(value);
    if (isNaN(num)) return value;
    switch (key) {
        case "current_price":
        case "last_price":
        case "open":
        case "avg_price":
            return "$" + num.toFixed(2);
        case "daily_change_dollar":
        case "ogap":
            return (num >= 0 ? "+" : "") + "$" + num.toFixed(2);
        case "daily_change_pct":
            return (num >= 0 ? "+" : "") + (num * 100).toFixed(2) + "%";
        default:
            return num.toLocaleString();
    }
}

function createSymbolLink(symbol) {
    const a = document.createElement("a");
    a.href = "https://www.tradingview.com/symbols/" + symbol + "/";
    a.target = "_blank";
    a.className = "symbol-link";
    a.innerHTML = symbol + ' <i class="fas fa-external-link-alt external-icon"></i>';
    return a;
}

function getSourceInfo(row, section) {
    // Extract source and data_age from the row based on section type
    // positions: source at [13], data_age at [14]
    // incomplete: source at [9], data_age at [10]
    // watchlist: source at [7], data_age at [8]
    let source = "ibkr", dataAge = "";
    if (section === "positions") {
        source = row[13] || "ibkr";
        dataAge = row[14] || "";
    } else if (section === "incomplete") {
        source = row[9] || "ibkr";
        dataAge = row[10] || "";
    } else if (section === "watchlist") {
        source = row[7] || "ibkr";
        dataAge = row[8] || "";
    }
    return { source, dataAge };
}

function createSourceDot(source, dataAge) {
    const dot = document.createElement("span");
    dot.className = "source-dot " + source;
    
    const sourceLabels = {
        ibkr: "IBKR Live",
        yahoo: "Yahoo Finance",
        cboe: "Cboe (delayed)",
        cached: "Cached Data",
        unavailable: "Unavailable"
    };
    
    const tooltip = document.createElement("span");
    tooltip.className = "price-tooltip";
    let tooltipHtml = '<div class="tooltip-source">' + (sourceLabels[source] || source) + '</div>';
    if (dataAge) {
        tooltipHtml += '<div class="tooltip-age">' + dataAge + '</div>';
    }
    tooltip.innerHTML = tooltipHtml;
    dot.appendChild(tooltip);
    
    return dot;
}

function createTable(data, section) {
    const config = getColumnConfig(section);
    const visibleCols = visibleColumnsForSection(section, config);

    const table = document.createElement("table");
    table.classList.add("data-table");
    table.dataset.section = section;

    const colgroup = document.createElement("colgroup");
    visibleCols.forEach(colDef => {
        const col = document.createElement("col");
        const width = config.widths[colDef.key] || defaultWidthFor(colDef.key);
        col.style.width = width + "px";
        colgroup.appendChild(col);
    });
    table.appendChild(colgroup);

    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    visibleCols.forEach(colDef => {
        const th = document.createElement("th");
        const label = document.createElement("span");
        label.className = "th-help";
        label.dataset.help = colDef.key;
        label.textContent = colDef.label;
        th.appendChild(label);
        th.dataset.colKey = colDef.key;
        th.classList.add("sortable");
        th.addEventListener("click", (e) => {
            if (e.target.closest(".th-resize-handle")) return;
            sortTable(table, section, th.cellIndex, colDef.key);
        });
        const handle = document.createElement("span");
        handle.className = "th-resize-handle";
        handle.title = "Drag to resize";
        handle.addEventListener("mousedown", (e) => startColumnResize(e, section, colDef.key, table));
        handle.addEventListener("click", (e) => e.stopPropagation());
        th.appendChild(handle);
        headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    data.sort((a, b) => a[0].toString().toLowerCase().localeCompare(b[0].toString().toLowerCase()));

    data.forEach(row => {
        const tr = document.createElement("tr");
        const { source, dataAge } = getSourceInfo(row, section);

        visibleCols.forEach(colDef => {
            const td = document.createElement("td");

            if (colDef.key === "data_source") {
                td.textContent = SOURCE_LABELS[source] || source;
                tr.appendChild(td);
                return;
            }

            const cell = row[colDef.rowIndex[section]];

            if (colDef.key === "underlying") {
                td.appendChild(createSymbolLink(cell));
            } else if (typeof cell === "number") {
                if (colDef.key === "current_price") {
                    // Add source indicator dot for Current Price column
                    const wrapper = document.createElement("span");
                    wrapper.className = "price-cell";
                    if (source === "cached") wrapper.classList.add("price-stale");

                    const priceText = document.createElement("span");
                    priceText.textContent = formatNumber(cell, colDef.key);
                    wrapper.appendChild(priceText);

                    // Only show dot when source is not IBKR (non-live data)
                    if (source !== "ibkr") {
                        wrapper.appendChild(createSourceDot(source, dataAge));
                    }

                    td.appendChild(wrapper);

                    const change = rowValue(row, "daily_change_dollar", section);
                    if (change > 0) td.classList.add("positive");
                    if (change < 0) td.classList.add("negative");
                } else {
                    td.textContent = formatNumber(cell, colDef.key);
                    if (["daily_change_dollar", "daily_change_pct", "ogap"].includes(colDef.key)) {
                        if (cell > 0) td.classList.add("positive");
                        if (cell < 0) td.classList.add("negative");
                    } else if (colDef.key === "shares" && cell === 0) {
                        td.classList.add("zero-shares");
                    } else if (colDef.key === "np" && cell > 0) {
                        td.classList.add("naked-puts");
                    } else if (colDef.key === "cc" && cell > 0) {
                        td.classList.add("covered-calls");
                    } else if (colDef.key === "uc" && cell > 0) {
                        td.classList.add("uncovered-calls");
                    } else if (colDef.key === "shares_available") {
                        if (cell > 0) td.classList.add("shares-available");
                        if (cell < 0) td.classList.add("shares-negative");
                    }
                }
            } else {
                td.textContent = cell;
            }
            tr.appendChild(td);
        });
        tbody.appendChild(tr);

        // Expandable option rows under positions that have option contracts
        if (section === "positions") {
            const symbol = row[0];
            const opts = optionsBySymbol[symbol];
            if (opts && opts.length > 0) {
                tr.classList.add("has-options");
                tr.dataset.symbol = symbol;
                const firstTd = tr.querySelector("td:first-child");
                const badge = document.createElement("span");
                badge.className = "opt-count-badge";
                badge.textContent = opts.length;
                badge.title = opts.length + " option contract(s) — click to expand";
                firstTd.appendChild(badge);

                // Surface a buyback opportunity without needing to expand the
                // row first -- previously only visible inside the option
                // sub-table.
                if (opts.some(o => o.buyback_target_hit)) {
                    const buybackBadge = document.createElement("span");
                    buybackBadge.className = "buyback-badge collapsed-hint";
                    buybackBadge.textContent = "BUYBACK";
                    buybackBadge.title = "At least one option here has hit its buyback threshold";
                    firstTd.appendChild(buybackBadge);
                }

                const expander = document.createElement("i");
                expander.className = "fas fa-chevron-right opt-expander";
                firstTd.appendChild(expander);

                const detail = buildOptionDetailRow(symbol, opts, visibleCols.length);
                detail.style.display = "none";
                tbody.appendChild(detail);

                firstTd.style.cursor = "pointer";
                firstTd.addEventListener("click", (e) => {
                    if (e.target.closest("a")) return; // symbol link still works
                    const open = detail.style.display !== "none";
                    detail.style.display = open ? "none" : "";
                    tr.classList.toggle("expanded", !open);
                });
            }
        }
    });
    table.appendChild(tbody);
    applySort(table, section);
    return table;
}

function startColumnResize(e, section, key, table) {
    e.preventDefault();
    e.stopPropagation();
    const handle = e.currentTarget;
    const th = handle.closest("th");
    const colIndex = th.cellIndex;
    const col = table.querySelector("colgroup").children[colIndex];
    const startX = e.clientX;
    const startWidth = col.getBoundingClientRect().width;
    handle.classList.add("active");
    th.classList.add("resizing");

    function onMove(ev) {
        const newWidth = Math.max(50, Math.round(startWidth + (ev.clientX - startX)));
        col.style.width = newWidth + "px";
    }
    function onUp() {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        handle.classList.remove("active");
        th.classList.remove("resizing");
        const finalWidth = Math.max(50, Math.round(col.getBoundingClientRect().width));
        const current = getColumnConfig(section);
        saveColumnConfig(section, { widths: { ...current.widths, [key]: finalWidth } });
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
}

function buildOptionDetailRow(symbol, opts, colspan) {
    const tr = document.createElement("tr");
    tr.className = "option-detail";
    tr.dataset.parent = symbol;
    const td = document.createElement("td");
    td.colSpan = colspan;

    let html = '<table class="option-subtable"><thead><tr>' +
        helpTh("Contract", "opt.contract") + helpTh("Pos", "opt.pos") +
        helpTh("Strike", "opt.strike") + helpTh("Expiry", "opt.expiry") +
        helpTh("DTE", "opt.dte") + helpTh("Delta", "opt.delta") +
        helpTh("Theta", "opt.theta") + helpTh("IV", "opt.iv") +
        helpTh("Entry", "opt.entry") + helpTh("Mark", "opt.mark") +
        helpTh("Prem. Left", "opt.prem_left") + '<th></th></tr></thead><tbody>';
    opts.forEach(o => {
        const short = (o.position || 0) < 0;
        const posClass = short ? "short-pos" : "long-pos";
        const premLeft = (o.premium_remaining_pct === null || o.premium_remaining_pct === undefined)
            ? "—" : o.premium_remaining_pct.toFixed(0) + "%";
        const fmt = (v, digits) => (v === null || v === undefined) ? "—" : Number(v).toFixed(digits);
        html += '<tr class="' + (o.buyback_target_hit ? "buyback-hit" : "") + '">' +
            '<td>' + escapeHtml((o.right === "P" ? "PUT" : "CALL")) + '</td>' +
            '<td class="' + posClass + '">' + escapeHtml(o.position) + '</td>' +
            '<td>$' + fmt(o.strike, 2) + '</td>' +
            '<td>' + escapeHtml(o.expiry || "—") + '</td>' +
            '<td>' + escapeHtml(o.dte !== null && o.dte !== undefined ? o.dte + "d" : "—") + '</td>' +
            '<td>' + fmt(o.delta, 2) + '</td>' +
            '<td>' + fmt(o.theta, 2) + '</td>' +
            '<td>' + (o.iv ? (o.iv * 100).toFixed(0) + "%" : "—") + '</td>' +
            '<td>$' + fmt(o.entry_price, 2) + '</td>' +
            '<td>$' + fmt(o.mark, 2) + '</td>' +
            '<td>' + premLeft + '</td>' +
            '<td>' + (o.buyback_target_hit ? '<span class="buyback-badge">BUYBACK TARGET</span>' : '') + '</td>' +
            '</tr>';
    });
    html += '</tbody></table>';
    td.innerHTML = html;
    tr.appendChild(td);
    return tr;
}

function sortTable(table, section, colIndex, key) {
    const current = currentSort[section];
    let dir = "asc";
    if (current && current.key === key) {
        dir = current.direction === "asc" ? "desc" : "asc";
    }
    currentSort[section] = { key, direction: dir };
    doSort(table, colIndex, key, dir);
}

// Re-applies whatever sort is already active for this section -- called after
// every createTable() rebuild (including periodic auto-refresh) so a chosen
// sort order survives instead of silently reverting to alphabetical.
function applySort(table, section) {
    const sort = currentSort[section];
    if (!sort) return;
    const th = Array.from(table.querySelectorAll("th")).find(t => t.dataset.colKey === sort.key);
    if (!th) return; // column got hidden since the sort was chosen
    doSort(table, th.cellIndex, sort.key, sort.direction);
}

function doSort(table, colIndex, key, dir) {
    const tbody = table.querySelector("tbody");
    // Direct children only -- querySelectorAll("tr:not(.option-detail)") would
    // also match rows *inside* the nested option-subtable (a plain <tr> with
    // no class), tearing them out of their own table and into this one.
    const topLevelRows = Array.from(tbody.children);
    const rows = topLevelRows.filter(tr => !tr.classList.contains("option-detail"));
    const details = {};
    topLevelRows.filter(tr => tr.classList.contains("option-detail")).forEach(d => {
        details[d.dataset.parent] = d;
    });

    table.querySelectorAll("th").forEach(h => h.classList.remove("asc", "desc"));
    const th = table.querySelector('th[data-col-key="' + key + '"]');
    if (th) th.classList.add(dir);

    rows.sort((a, b) => {
        const aVal = getCellValue(a, colIndex);
        const bVal = getCellValue(b, colIndex);
        if (isNaN(parseFloat(aVal)) || isNaN(parseFloat(bVal))) {
            return dir === "asc" ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
        }
        return dir === "asc" ? parseFloat(aVal) - parseFloat(bVal) : parseFloat(bVal) - parseFloat(aVal);
    });

    rows.forEach(row => {
        tbody.appendChild(row);
        const detail = row.dataset.symbol && details[row.dataset.symbol];
        if (detail) tbody.appendChild(detail);
    });
}

function getCellValue(row, index) {
    const cell = row.querySelector("td:nth-child(" + (index + 1) + ")");
    return cell ? cell.textContent.trim().replace(/[$%+,]/g, "") : "";
}

function filterTables(searchText) {
    document.querySelectorAll("#tab-positions table:not(.option-subtable)").forEach(table => {
        const tbody = table.querySelector("tbody");
        if (!tbody) return;
        // Direct children only -- querySelectorAll("tbody tr:not(.option-detail)")
        // would also reach rows inside the nested option-subtable and filter
        // those independently by symbol text, which is wrong.
        const rows = Array.from(tbody.children).filter(tr => !tr.classList.contains("option-detail"));
        let hasVisible = false;

        rows.forEach(row => {
            const symbol = row.querySelector("td:first-child")?.textContent || "";
            if (symbol.match(new RegExp(searchText, "i"))) {
                row.classList.remove("hidden");
                hasVisible = true;
            } else {
                row.classList.add("hidden");
            }
            const detail = row.dataset.symbol
                ? table.querySelector('tr.option-detail[data-parent="' + row.dataset.symbol + '"]')
                : null;
            if (detail) detail.classList.toggle("hidden", row.classList.contains("hidden"));
        });
        
        let noResults = table.parentElement.querySelector(".no-results");
        if (hasVisible || rows.length === 0) {
            if (noResults) noResults.style.display = "none";
        } else {
            if (!noResults) {
                noResults = document.createElement("div");
                noResults.className = "no-results";
                noResults.textContent = "No matching symbols found";
                table.parentElement.appendChild(noResults);
            }
            noResults.style.display = "block";
        }
    });
}

function clearSearch() {
    const input = document.getElementById("searchInput");
    input.value = "";
    filterTables("");
    input.focus();
}

function updateSummaryStats(data) {
    document.getElementById("statPositions").textContent = data.positions.length;
    document.getElementById("statWatchlist").textContent = data.watchlist.length;
    
    let gainers = 0, losers = 0, dailyPL = 0;
    data.positions.forEach(pos => {
        const change = pos[4];
        if (change > 0) gainers++;
        if (change < 0) losers++;
        dailyPL += change * pos[1];
    });
    
    document.getElementById("statGainers").textContent = gainers;
    document.getElementById("statLosers").textContent = losers;
    
    const plEl = document.getElementById("statDailyPL");
    plEl.textContent = (dailyPL >= 0 ? "+" : "") + "$" + dailyPL.toFixed(2);
    plEl.className = "stat-value " + (dailyPL >= 0 ? "positive" : "negative");
}

function updateSectionCounts(data) {
    document.getElementById("positions-count").textContent = data.positions.length;
    document.getElementById("incomplete-count").textContent = data.incomplete_lots.length;
    document.getElementById("watchlist-count").textContent = data.watchlist.length;
}

function updateLastUpdateTime() {
    document.getElementById("lastUpdate").innerHTML = '<i class="far fa-clock"></i> <span>Updated at ' + new Date().toLocaleTimeString() + '</span>';
}

function setLoadingState(loading) {
    const icon = document.querySelector(".refresh-icon");
    if (loading) {
        icon.classList.add("refreshing");
    } else {
        icon.classList.remove("refreshing");
    }
}

const SOURCE_LABELS = {
    ibkr: "IBKR", yahoo: "Yahoo Finance", cboe: "Cboe",
    cached: "cached data", unavailable: "unavailable",
};

// Toasts only on an actual IBKR<->fallback transition, not on every poll --
// updateConnectionStatus() already shows the current source persistently.
function notifyDataSourceChange(data) {
    const source = data.connection_source || "ibkr";
    if (lastConnectionSource !== null && lastConnectionSource !== source) {
        if (data.fallback) {
            showToast(data.fallback_message || ("Switched to " + (SOURCE_LABELS[source] || source)),
                "info", 4000, "dataSource");
        } else {
            showToast("Reconnected to IBKR", "success", 3000, "dataSource");
        }
    }
    lastConnectionSource = source;
}

function updateConnectionStatus(data) {
    const statusEl = document.getElementById("connectionStatus");
    const source = data.connection_source || "ibkr";
    
    // Remove any existing fallback banner
    const existingBanner = document.querySelector(".fallback-banner");
    if (existingBanner) existingBanner.remove();
    
    if (data.fallback) {
        // Show fallback banner with a "Why?" link to open diagnostics
        const banner = document.createElement("div");
        banner.className = "fallback-banner";
        const msg = data.fallback_message || "Using fallback data";
        banner.innerHTML = '<i class="fas fa-exclamation-triangle"></i> <span></span> <button type="button" class="fallback-why-link">Why?</button>';
        banner.querySelector("span").textContent = msg;
        banner.querySelector(".fallback-why-link").addEventListener("click", openDiagnosticsModal);
        const header = document.querySelector(".header");
        header.parentElement.insertBefore(banner, header.nextSibling);

        statusEl.classList.remove("connected", "disconnected");
        statusEl.classList.add("fallback", "clickable");
        statusEl.innerHTML = '<i class="fas fa-plug"></i> ' + (source === "yahoo" ? "Yahoo Finance" : "Cached Data");
    } else if (source === "ibkr") {
        statusEl.classList.remove("disconnected", "fallback");
        statusEl.classList.add("connected", "clickable");
        statusEl.innerHTML = '<i class="fas fa-plug"></i> IBKR Connected';
    } else if (source === "yahoo") {
        statusEl.classList.remove("connected", "disconnected");
        statusEl.classList.add("fallback", "clickable");
        statusEl.innerHTML = '<i class="fas fa-plug"></i> Yahoo Finance';
    } else if (source === "cboe") {
        statusEl.classList.remove("connected", "disconnected");
        statusEl.classList.add("fallback", "clickable");
        statusEl.innerHTML = '<i class="fas fa-plug"></i> Cboe';
    } else if (source === "cached") {
        statusEl.classList.remove("connected", "disconnected");
        statusEl.classList.add("fallback", "clickable");
        statusEl.innerHTML = '<i class="fas fa-plug"></i> Cached Data';
    }
}

async function updateTables() {
    log("updateTables called");
    if (isRefreshing) {
        log("Already refreshing, skipping");
        return;
    }
    // The header a tooltip is anchored to gets destroyed by the rebuild below,
    // which would leave the tooltip floating over nothing. It comes straight
    // back on the next mouse move over the new header.
    hideHelpTip();
    
    isRefreshing = true;
    setLoadingState(true);
    log("Starting data fetch...");
    
    try {
        log("Fetching /api/data...");
        const response = await fetch("/api/data");
        log("Response status: " + response.status);
        
        const data = await response.json();
        
        // Handle error responses that may still contain fallback data
        if (!response.ok && !data.fallback) {
            throw new Error(data.error || "Server error");
        }
        
        log("Data received: " + data.positions.length + " positions, " + data.watchlist.length + " watchlist");
        cachedData = data;

        const positionsTable = document.getElementById("positions-table");
        const incompleteTable = document.getElementById("incomplete-table");
        const watchlistTable = document.getElementById("watchlist-table");

        optionsBySymbol = data.options_by_symbol || {};

        positionsTable.innerHTML = data.positions.length > 0 ? "" : '<div class="no-results">No positions found</div>';
        incompleteTable.innerHTML = data.incomplete_lots.length > 0 ? "" : '<div class="no-results">No incomplete lots</div>';
        watchlistTable.innerHTML = data.watchlist.length > 0 ? "" : '<div class="no-results">No watchlist items</div>';

        if (data.positions.length > 0) positionsTable.appendChild(createTable(data.positions, "positions"));
        if (data.incomplete_lots.length > 0) incompleteTable.appendChild(createTable(data.incomplete_lots, "incomplete"));
        if (data.watchlist.length > 0) watchlistTable.appendChild(createTable(data.watchlist, "watchlist"));
        
        updateLastUpdateTime();
        updateSummaryStats(data);
        updateSectionCounts(data);
        updateConnectionStatus(data);
        
        const searchVal = document.getElementById("searchInput").value;
        if (searchVal) filterTables(searchVal);
        
        notifyDataSourceChange(data);
        if (!data.fallback) {
            showToast("Data refreshed", "success", 1500, "refresh");
        }

    } catch (error) {
        log("Error: " + error.message);
        console.error("Fetch error:", error);
        showToast(error.message, "error", 3000, "errors");
        const csEl = document.getElementById("connectionStatus");
        csEl.classList.remove("connected", "fallback");
        csEl.classList.add("disconnected", "clickable");
        csEl.innerHTML = '<i class="fas fa-plug"></i> Connection Error';
    } finally {
        isRefreshing = false;
        setLoadingState(false);
    }
}

function setRefreshRate(seconds) {
    if (refreshInterval) clearInterval(refreshInterval);
    if (seconds > 0) {
        refreshInterval = setInterval(updateTables, seconds * 1000);
    }
    savePreferences({ refreshRate: seconds });
}

// Keyboard shortcuts
document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") {
        if (e.key === "Escape") {
            if (e.target.id === "trancheSearchInput") {
                clearTrancheSearch();
            } else {
                clearSearch();
            }
            e.target.blur();
        }
        return;
    }
    
    switch (e.key.toLowerCase()) {
        case "r":
            e.preventDefault();
            updateTables();
            break;
        case "/":
            e.preventDefault();
            document.getElementById("searchInput").focus();
            break;
        case "d":
            e.preventDefault();
            toggleDarkMode();
            break;
        case "c":
            e.preventDefault();
            toggleCompactView();
            break;
        case "e":
            e.preventDefault();
            exportToCSV();
            break;
        case "?":
            e.preventDefault();
            openShortcutsModal();
            break;
        case "1":
            switchTab("positions");
            break;
        case "2":
            switchTab("tranches");
            break;
        case "3":
            switchTab("income");
            break;
        case "4":
            switchTab("settings");
            break;
        case "escape":
            closeShortcutsModal();
            closeDiagnosticsModal();
            break;
    }
});

// ============ Tabs ============
const TAB_LOADERS = {
    tranches: () => typeof loadTranches === "function" && loadTranches(),
    income: () => typeof loadIncome === "function" && loadIncome(),
    settings: () => typeof loadSettings === "function" && loadSettings(),
};

function switchTab(name) {
    document.querySelectorAll(".tab-btn").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.tab === name);
    });
    document.querySelectorAll(".tab-panel").forEach(panel => {
        panel.classList.toggle("active", panel.id === "tab-" + name);
    });
    savePreferences({ activeTab: name });
    // Lazy-load each tab's data on first visit
    if (!loadedTabs[name] && TAB_LOADERS[name]) {
        loadedTabs[name] = true;
        TAB_LOADERS[name]();
    }
}

function initTabs() {
    document.querySelectorAll(".tab-btn").forEach(btn => {
        btn.addEventListener("click", () => switchTab(btn.dataset.tab));
    });
    const prefs = loadPreferences();
    if (prefs.activeTab && prefs.activeTab !== "positions") {
        switchTab(prefs.activeTab);
    }
}

// Initialize on DOM ready
document.addEventListener("DOMContentLoaded", () => {
    log("DOM loaded, initializing...");
    
    applyPreferences();
    
    document.getElementById("darkModeToggle").addEventListener("click", toggleDarkMode);
    document.getElementById("compactToggle").addEventListener("click", toggleCompactView);
    document.getElementById("exportBtn").addEventListener("click", exportToCSV);
    document.getElementById("shortcutsBtn").addEventListener("click", openShortcutsModal);
    document.getElementById("refreshButton").addEventListener("click", () => {
        log("Refresh button clicked");
        updateTables();
    });
    document.getElementById("refreshRate").addEventListener("change", (e) => setRefreshRate(parseInt(e.target.value)));
    document.getElementById("searchInput").addEventListener("input", (e) => filterTables(e.target.value));
    document.getElementById("clearSearch").addEventListener("click", clearSearch);
    document.getElementById("shortcuts-modal").addEventListener("click", (e) => {
        if (e.target === document.getElementById("shortcuts-modal")) closeShortcutsModal();
    });
    document.getElementById("connectionStatus").addEventListener("click", openDiagnosticsModal);
    document.getElementById("diagnostics-modal").addEventListener("click", (e) => {
        if (e.target === document.getElementById("diagnostics-modal")) closeDiagnosticsModal();
    });
    document.getElementById("diagnosticsRefreshBtn").addEventListener("click", loadDiagnostics);
    document.getElementById("columns-modal").addEventListener("click", (e) => {
        if (e.target === document.getElementById("columns-modal")) closeColumnsModal();
    });
    document.getElementById("columnsResetBtn").addEventListener("click", resetColumnsToDefault);

    initTabs();

    updateMarketStatus();
    marketStatusInterval = setInterval(updateMarketStatus, 60000);
    
    log("Calling initial updateTables...");
    updateTables();
    
    setRefreshRate(parseInt(document.getElementById("refreshRate").value));
    
    // Check for updates after a delay
    setTimeout(checkForUpdates, 2000);
});

// Update notification functions
function showUpdateNotification(version, notes) {
    const banner = document.createElement("div");
    banner.id = "update-banner";
    banner.style.cssText = "position:fixed;top:0;left:0;right:0;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:white;padding:12px 20px;display:flex;align-items:center;justify-content:center;gap:16px;z-index:10001;font-family:var(--font-sans);box-shadow:0 4px 12px rgba(0,0,0,0.15);";
    banner.innerHTML = '<i class="fas fa-gift" style="font-size:20px"></i><span><strong>Update Available!</strong> Version ' + escapeHtml(version) + ' is ready.</span><button onclick="installUpdate()" style="background:white;color:#3b82f6;border:none;padding:8px 16px;border-radius:6px;font-weight:600;cursor:pointer">Update Now</button><button onclick="dismissUpdate()" style="background:transparent;color:white;border:1px solid rgba(255,255,255,0.5);padding:8px 12px;border-radius:6px;cursor:pointer">Later</button>';
    document.body.prepend(banner);
    document.querySelector(".container").style.marginTop = "60px";
}

function dismissUpdate() {
    const banner = document.getElementById("update-banner");
    if (banner) banner.remove();
    document.querySelector(".container").style.marginTop = "";
}

async function installUpdate() {
    showToast("Downloading update...", "info", 5000);
    try {
        const response = await fetch("/api/update/download");
        const result = await response.json();
        if (result.success) {
            showToast("Installing update... The app will restart.", "success", 10000);
        } else {
            showToast("Update failed: " + result.error, "error");
        }
    } catch (error) {
        showToast("Update failed: " + error.message, "error");
    }
}

async function checkForUpdates() {
    try {
        log("Checking for updates...");
        const response = await fetch("/api/update/check");
        const result = await response.json();
        if (result.available) {
            log("Update available: " + result.latest_version);
            showUpdateNotification(result.latest_version, result.release_notes || "");
        } else {
            log("No updates available");
        }
    } catch (error) {
        log("Could not check for updates: " + error);
    }
}
