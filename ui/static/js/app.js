
// TTC Positions Report - Frontend JavaScript (core + Positions tab)
// Version 2.3.0

let isRefreshing = false;
let currentSort = { column: null, direction: "asc" };
let refreshInterval;
let cachedData = null;
let optionsBySymbol = {};
let marketStatusInterval;
const PREFS_KEY = "ttc_positions_prefs";
const loadedTabs = { positions: true };

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
}

function showToast(message, type = "info", duration = 3000) {
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
        showToast("No data to export", "error");
        return;
    }
    let csv = ["Symbol","Shares","Current Price","Avg Price","Daily Change $","Daily Change %","Last Price","Open","OGap","NP","CC","UC","Shares Available","Data Source"].join(",") + "\n";
    cachedData.positions.forEach(row => {
        csv += row.slice(0, 14).map((val, i) => i === 5 ? (val * 100).toFixed(2) + "%" : (typeof val === "number" ? val.toFixed(2) : val)).join(",") + "\n";
    });
    const blob = new Blob([csv], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "ttc_positions_" + new Date().toISOString().split("T")[0] + ".csv";
    a.click();
    showToast("Exported to CSV", "success");
}

function formatNumber(value, column) {
    if (value === "" || value === null || value === undefined) return "";
    const num = parseFloat(value);
    if (isNaN(num)) return value;
    switch (column) {
        case "Current Price":
        case "Last Price":
        case "Open":
        case "Avg Price":
            return "$" + num.toFixed(2);
        case "Daily Change $":
        case "OGap":
            return (num >= 0 ? "+" : "") + "$" + num.toFixed(2);
        case "Daily Change %":
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

function createTable(data, headers, section) {
    const table = document.createElement("table");
    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    
    // Display headers exclude source metadata columns
    let sectionHeaders = headers;
    if (section === "incomplete") {
        sectionHeaders = headers.filter(h => !["NP", "CC", "UC", "Shares Available"].includes(h));
    } else if (section === "watchlist") {
        sectionHeaders = ["Underlying", "Current Price", "Daily Change $", "Daily Change %", "Last Price", "Open", "OGap"];
    }
    
    sectionHeaders.forEach((header, idx) => {
        const th = document.createElement("th");
        th.textContent = header;
        th.classList.add("sortable");
        th.addEventListener("click", () => sortTable(table, idx, header));
        headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);
    
    const tbody = document.createElement("tbody");
    data.sort((a, b) => a[0].toString().toLowerCase().localeCompare(b[0].toString().toLowerCase()));
    
    data.forEach(row => {
        const tr = document.createElement("tr");
        const { source, dataAge } = getSourceInfo(row, section);
        
        let rowData = [...row];
        if (section === "incomplete") {
            rowData = rowData.filter((_, i) => !["NP", "CC", "UC", "Shares Available"].includes(headers[i]));
        } else if (section === "watchlist") {
            rowData = rowData.slice(0, 7);
        }
        
        rowData.forEach((cell, idx) => {
            const td = document.createElement("td");
            const header = sectionHeaders[idx];
            
            if (header === "Underlying") {
                td.appendChild(createSymbolLink(cell));
            } else if (typeof cell === "number") {
                if (header === "Current Price") {
                    // Add source indicator dot for Current Price column
                    const wrapper = document.createElement("span");
                    wrapper.className = "price-cell";
                    if (source === "cached") wrapper.classList.add("price-stale");
                    
                    const priceText = document.createElement("span");
                    priceText.textContent = formatNumber(cell, header);
                    wrapper.appendChild(priceText);
                    
                    // Only show dot when source is not IBKR (non-live data)
                    if (source !== "ibkr") {
                        wrapper.appendChild(createSourceDot(source, dataAge));
                    }
                    
                    td.appendChild(wrapper);
                    
                    const changeIdx = headers.indexOf("Daily Change $");
                    if (row[changeIdx] > 0) td.classList.add("positive");
                    if (row[changeIdx] < 0) td.classList.add("negative");
                } else {
                    td.textContent = formatNumber(cell, header);
                    if (["Daily Change $", "Daily Change %", "OGap"].includes(header)) {
                        if (cell > 0) td.classList.add("positive");
                        if (cell < 0) td.classList.add("negative");
                    } else if (header === "Shares" && cell === 0) {
                        td.classList.add("zero-shares");
                    } else if (header === "NP" && cell > 0) {
                        td.classList.add("naked-puts");
                    } else if (header === "CC" && cell > 0) {
                        td.classList.add("covered-calls");
                    } else if (header === "UC" && cell > 0) {
                        td.classList.add("uncovered-calls");
                    } else if (header === "Shares Available") {
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
                const expander = document.createElement("i");
                expander.className = "fas fa-chevron-right opt-expander";
                firstTd.appendChild(badge);
                firstTd.appendChild(expander);

                const detail = buildOptionDetailRow(symbol, opts, sectionHeaders.length);
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
    return table;
}

function buildOptionDetailRow(symbol, opts, colspan) {
    const tr = document.createElement("tr");
    tr.className = "option-detail";
    tr.dataset.parent = symbol;
    const td = document.createElement("td");
    td.colSpan = colspan;

    let html = '<table class="option-subtable"><thead><tr>' +
        '<th>Contract</th><th>Pos</th><th>Strike</th><th>Expiry</th><th>DTE</th>' +
        '<th>Delta</th><th>Theta</th><th>IV</th><th>Entry</th><th>Mark</th>' +
        '<th>Prem. Left</th><th></th></tr></thead><tbody>';
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

function sortTable(table, colIndex) {
    const tbody = table.querySelector("tbody");
    // Option-detail rows travel with their parent row, never sort on their own
    const rows = Array.from(tbody.querySelectorAll("tr:not(.option-detail)"));
    const details = {};
    tbody.querySelectorAll("tr.option-detail").forEach(d => {
        details[d.dataset.parent] = d;
    });
    const th = table.querySelector("th:nth-child(" + (colIndex + 1) + ")");

    table.querySelectorAll("th").forEach(h => h.classList.remove("asc", "desc"));

    let dir = "asc";
    if (currentSort.column === colIndex) {
        dir = currentSort.direction === "asc" ? "desc" : "asc";
    }
    currentSort = { column: colIndex, direction: dir };
    th.classList.add(dir);

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
        const rows = table.querySelectorAll("tbody tr:not(.option-detail)");
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
        
        const headers = ["Underlying", "Shares", "Current Price", "Avg Price", "Daily Change $", "Daily Change %", "Last Price", "Open", "OGap", "NP", "CC", "UC", "Shares Available"];
        
        const positionsTable = document.getElementById("positions-table");
        const incompleteTable = document.getElementById("incomplete-table");
        const watchlistTable = document.getElementById("watchlist-table");
        
        optionsBySymbol = data.options_by_symbol || {};

        positionsTable.innerHTML = data.positions.length > 0 ? "" : '<div class="no-results">No positions found</div>';
        incompleteTable.innerHTML = data.incomplete_lots.length > 0 ? "" : '<div class="no-results">No incomplete lots</div>';
        watchlistTable.innerHTML = data.watchlist.length > 0 ? "" : '<div class="no-results">No watchlist items</div>';
        
        if (data.positions.length > 0) positionsTable.appendChild(createTable(data.positions, headers, "positions"));
        if (data.incomplete_lots.length > 0) incompleteTable.appendChild(createTable(data.incomplete_lots, headers, "incomplete"));
        if (data.watchlist.length > 0) watchlistTable.appendChild(createTable(data.watchlist, headers, "watchlist"));
        
        updateLastUpdateTime();
        updateSummaryStats(data);
        updateSectionCounts(data);
        updateConnectionStatus(data);
        
        const searchVal = document.getElementById("searchInput").value;
        if (searchVal) filterTables(searchVal);
        
        if (data.fallback) {
            showToast(data.fallback_message || "Using fallback data", "info", 3000);
        } else {
            showToast("Data refreshed", "success", 1500);
        }
        
    } catch (error) {
        log("Error: " + error.message);
        console.error("Fetch error:", error);
        showToast(error.message, "error");
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
            clearSearch();
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
