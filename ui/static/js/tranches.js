// Tranches tab: per-lot wheel tracking rendered from /api/tranches

function fmtMoney(v, signed) {
    if (v === null || v === undefined) return "—";
    const n = Number(v);
    const sign = signed && n > 0 ? "+" : "";
    return sign + "$" + n.toFixed(2);
}

function fmtDate(iso) {
    if (!iso) return "—";
    try {
        return new Date(iso).toLocaleDateString();
    } catch (e) {
        return iso.split("T")[0];
    }
}

async function loadTranches() {
    const container = document.getElementById("tranches-content");
    container.innerHTML = '<div class="skeleton-loader"><div class="skeleton-row"></div><div class="skeleton-row"></div></div>';
    try {
        const response = await fetch("/api/tranches");
        const data = await response.json();
        if (data.error) throw new Error(data.error);
        renderTranches(data);
    } catch (err) {
        container.innerHTML = '<div class="empty-state"><i class="fas fa-triangle-exclamation"></i>Could not load tranches: '
            + escapeHtml(err.message) + '</div>';
    }
}

function renderTranches(data) {
    const container = document.getElementById("tranches-content");

    if (!data.flex_configured && data.trade_count === 0) {
        container.innerHTML =
            '<div class="empty-state"><i class="fas fa-plug-circle-bolt"></i>' +
            'Tranche tracking needs your IBKR trade history.<br>' +
            'Set up the one-time Flex Query connection in ' +
            '<a onclick="switchTab(\'settings\')">Settings</a> — it takes about 5 minutes.</div>';
        return;
    }
    if (!data.groups || data.groups.length === 0) {
        container.innerHTML =
            '<div class="empty-state"><i class="fas fa-layer-group"></i>' +
            'No tranches yet. ' + data.trade_count + ' trades imported — ' +
            'tranches appear once there are stock buys or put assignments in the history.</div>';
        return;
    }

    container.innerHTML = "";
    data.groups.forEach(group => {
        if (group.open.length === 0 && group.closed.length === 0) return;
        const el = document.createElement("div");
        el.className = "tranche-group";

        const openRows = group.open.map(t => {
            const badges = [];
            if (t.inferred) badges.push('<span class="badge seeded" title="Opened before the imported history; entry price is IBKR’s average cost">SEEDED</span>');
            if (t.open_source === "PUT_ASSIGNMENT") badges.push('<span class="badge assignment">PUT ASSIGNED</span>');
            let coverCell = '—';
            if (t.covering_call) {
                coverCell = '<span class="badge covered">CALL $' + Number(t.covering_call.strike || 0).toFixed(2)
                    + ' ' + escapeHtml(t.covering_call.expiry || '') + '</span>'
                    + ' <span class="badge uncover-warn" title="Selling these shares would leave this written call uncovered"><i class="fas fa-triangle-exclamation"></i> DON’T SELL SHARES</span>';
            }
            const plClass = (t.unrealized_pl || 0) >= 0 ? "positive" : "negative";
            return '<tr>' +
                '<td>' + fmtDate(t.opened_ts) + ' ' + badges.join(' ') + '</td>' +
                '<td>' + t.qty + '</td>' +
                '<td>' + fmtMoney(t.open_price) + '</td>' +
                '<td>' + fmtMoney(t.premium, true) + '</td>' +
                '<td>' + fmtMoney(t.net_basis) + '</td>' +
                '<td>' + (t.current_price ? fmtMoney(t.current_price) : '—') + '</td>' +
                '<td class="' + plClass + '">' + fmtMoney(t.unrealized_pl, true) + '</td>' +
                '<td>' + coverCell + '</td>' +
                '</tr>';
        }).join("");

        const closedRows = group.closed.map(t => {
            const plClass = (t.realized_pl || 0) >= 0 ? "positive" : "negative";
            const how = t.close_source === "CALL_ASSIGNMENT"
                ? '<span class="badge assignment">CALLED AWAY</span>' : 'sold';
            return '<tr>' +
                '<td>' + fmtDate(t.opened_ts) + '</td>' +
                '<td>' + t.qty + '</td>' +
                '<td>' + fmtMoney(t.open_price) + '</td>' +
                '<td>' + fmtMoney(t.premium, true) + '</td>' +
                '<td>' + fmtDate(t.closed_ts) + '</td>' +
                '<td>' + fmtMoney(t.close_price) + '</td>' +
                '<td>' + how + '</td>' +
                '<td class="' + plClass + '">' + fmtMoney(t.realized_pl, true) + '</td>' +
                '</tr>';
        }).join("");

        let html =
            '<div class="tranche-group-header">' +
                '<h3>' + escapeHtml(group.symbol) + '</h3>' +
                '<div class="tranche-group-stats">' +
                    '<span>Open: <b>' + group.open_shares + ' sh</b></span>' +
                    '<span>Premium: <b>' + fmtMoney(group.total_premium, true) + '</b></span>' +
                    '<span>Realized: <b>' + fmtMoney(group.realized_pl, true) + '</b></span>' +
                '</div>' +
            '</div>';

        if (group.open.length > 0) {
            html += '<div class="table-container"><table class="tranche-table">' +
                '<thead><tr><th>Opened</th><th>Qty</th><th>Open Price</th><th>Premium</th>' +
                '<th>Net Basis</th><th>Current</th><th>Unrealized P/L</th><th>Covering Call</th></tr></thead>' +
                '<tbody>' + openRows + '</tbody></table></div>';
        }
        if (group.closed.length > 0) {
            html += '<div class="tranche-closed-toggle"><i class="fas fa-chevron-right"></i> ' +
                group.closed.length + ' closed tranche(s) — click to show</div>' +
                '<div class="table-container closed-tranches" style="display:none">' +
                '<table class="tranche-table">' +
                '<thead><tr><th>Opened</th><th>Qty</th><th>Open Price</th><th>Premium</th>' +
                '<th>Closed</th><th>Close Price</th><th>How</th><th>Realized P/L</th></tr></thead>' +
                '<tbody>' + closedRows + '</tbody></table></div>';
        }

        el.innerHTML = html;
        const toggle = el.querySelector(".tranche-closed-toggle");
        if (toggle) {
            toggle.addEventListener("click", () => {
                const panel = el.querySelector(".closed-tranches");
                const shown = panel.style.display !== "none";
                panel.style.display = shown ? "none" : "";
                toggle.querySelector("i").className = shown
                    ? "fas fa-chevron-right" : "fas fa-chevron-down";
            });
        }
        container.appendChild(el);
    });

    const note = document.createElement("div");
    note.className = "panel-note";
    note.style.marginTop = "6px";
    note.innerHTML = '<i class="fas fa-circle-info"></i> Built from ' + data.trade_count +
        ' imported trades' +
        (data.last_import && data.last_import.requested_ts
            ? ' — last import ' + fmtDate(data.last_import.requested_ts) : '') + '.';
    container.appendChild(note);
}

document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("tranchesRefreshBtn");
    if (btn) btn.addEventListener("click", loadTranches);
});
