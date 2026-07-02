// Income tab: premium collected and realized P/L from /api/income

async function loadIncome() {
    const container = document.getElementById("income-content");
    container.innerHTML = '<div class="skeleton-loader"><div class="skeleton-row"></div><div class="skeleton-row"></div></div>';
    try {
        const response = await fetch("/api/income");
        const data = await response.json();
        if (data.error) throw new Error(data.error);
        renderIncome(data);
    } catch (err) {
        container.innerHTML = '<div class="empty-state"><i class="fas fa-triangle-exclamation"></i>Could not load income: '
            + escapeHtml(err.message) + '</div>';
    }
}

function renderIncome(data) {
    const container = document.getElementById("income-content");

    if (!data.flex_configured && data.trade_count === 0) {
        container.innerHTML =
            '<div class="empty-state"><i class="fas fa-sack-dollar"></i>' +
            'Income tracking needs your IBKR trade history.<br>' +
            'Set up the one-time Flex Query connection in ' +
            '<a onclick="switchTab(\'settings\')">Settings</a>.</div>';
        return;
    }

    const weekly = data.weekly_premium || [];
    const monthly = data.monthly_premium || [];
    const thisWeek = weekly.length ? weekly[0] : null;
    const thisMonth = monthly.length ? monthly[0] : null;
    const goal = data.weekly_goal || 0;

    let cards = '<div class="income-cards">';
    cards += incomeCard('Premium this week', thisWeek ? thisWeek.amount : 0, goal);
    cards += incomeCard('Premium this month', thisMonth ? thisMonth.amount : 0, 0);
    cards += incomeCard('Realized P/L (closed tranches)', data.realized_pl_closed || 0, 0);
    cards += '</div>';

    const maxAmount = Math.max(1, ...weekly.slice(0, 12).map(w => Math.abs(w.amount)));
    const weekRows = weekly.slice(0, 12).map(w => {
        const width = Math.max(2, Math.round(100 * Math.abs(w.amount) / maxAmount));
        const cls = w.amount < 0 ? "bar negative" : "bar";
        const goalMark = goal > 0
            ? (w.amount >= goal ? ' <i class="fas fa-circle-check positive" title="Goal met"></i>' : '')
            : '';
        return '<tr><td>' + escapeHtml(w.period) + '</td>' +
            '<td class="' + (w.amount >= 0 ? 'positive' : 'negative') + '">' + fmtMoney(w.amount, true) + goalMark + '</td>' +
            '<td style="width:50%"><div class="period-bar"><div class="' + cls + '" style="width:' + width + '%"></div></div></td></tr>';
    }).join("");

    const monthRows = monthly.slice(0, 12).map(m =>
        '<tr><td>' + escapeHtml(m.period) + '</td>' +
        '<td class="' + (m.amount >= 0 ? 'positive' : 'negative') + '">' + fmtMoney(m.amount, true) + '</td><td></td></tr>'
    ).join("");

    const assignments = (data.assignments || []).slice(-15).reverse().map(a =>
        '<tr><td>' + fmtDate(a.ts) + '</td><td>' + escapeHtml(a.symbol) + '</td>' +
        '<td>' + escapeHtml(a.event_type === 'PUT_ASSIGNED' ? 'Put assigned (shares in)' : 'Called away (shares out)') + '</td>' +
        '<td>' + fmtMoney(a.amount, true) + '</td></tr>'
    ).join("");

    container.innerHTML = cards +
        '<div class="sections-container">' +
        section('Premium by week', 'fa-calendar-week',
            table(['Week', 'Premium', ''], weekRows, 'No premium recorded yet.')) +
        section('Premium by month', 'fa-calendar',
            table(['Month', 'Premium', ''], monthRows, 'No premium recorded yet.')) +
        section('Recent assignments', 'fa-right-left',
            table(['Date', 'Symbol', 'What happened', 'Amount'], assignments,
                  'No assignments in the imported history.')) +
        '</div>';
}

function incomeCard(label, amount, goal) {
    let goalHtml = '';
    if (goal > 0) {
        const pct = Math.min(100, Math.max(0, Math.round(100 * amount / goal)));
        goalHtml = '<div class="goal-bar"><div class="goal-bar-fill" style="width:' + pct + '%"></div></div>' +
            '<span class="form-hint">' + pct + '% of $' + goal.toFixed(0) + ' weekly goal</span>';
    }
    const cls = amount >= 0 ? 'positive' : 'negative';
    return '<div class="income-card"><span class="label">' + escapeHtml(label) + '</span>' +
        '<span class="value ' + cls + '">' + fmtMoney(amount, true) + '</span>' + goalHtml + '</div>';
}

function section(title, icon, body) {
    return '<section class="section"><div class="section-header static">' +
        '<h2><i class="fas ' + icon + '"></i> ' + escapeHtml(title) + '</h2></div>' +
        '<div class="section-content"><div class="table-container">' + body + '</div></div></section>';
}

function table(headers, rows, emptyMessage) {
    if (!rows) {
        return '<div class="no-results">' + escapeHtml(emptyMessage) + '</div>';
    }
    return '<table><thead><tr>' +
        headers.map(h => '<th style="text-align:left">' + escapeHtml(h) + '</th>').join('') +
        '</tr></thead><tbody>' + rows + '</tbody></table>';
}

document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("incomeRefreshBtn");
    if (btn) btn.addEventListener("click", loadIncome);
});
