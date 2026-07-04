// Income tab: premium collected, realized/unrealized P/L, and outcome
// breakdowns from /api/income

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
    const weeklyGoal = data.weekly_goal || 0;
    const monthlyGoal = data.monthly_goal || 0;

    let cards = '<div class="income-cards">';
    cards += incomeCard('Premium this week', thisWeek ? thisWeek.amount : 0, weeklyGoal, data.weekly_streak);
    cards += incomeCard('Premium this month', thisMonth ? thisMonth.amount : 0, monthlyGoal, data.monthly_streak);
    cards += incomeCard('Realized P/L (closed tranches)', data.realized_pl_closed || 0, 0);
    cards += incomeCard('Unrealized P/L (open tranches)', data.unrealized_pl_open, 0);
    cards += '</div>';

    const trendChart = renderTrendChart(weekly, weeklyGoal);

    const weekRows = weekly.slice(0, 12).map(w => {
        const goalMark = weeklyGoal > 0
            ? (w.amount >= weeklyGoal ? ' <i class="fas fa-circle-check positive" title="Goal met"></i>' : '')
            : '';
        return '<tr><td>' + escapeHtml(w.period) + '</td>' +
            '<td class="' + (w.amount >= 0 ? 'positive' : 'negative') + '">' + fmtMoney(w.amount, true) + goalMark + '</td></tr>';
    }).join("");

    const monthRows = monthly.slice(0, 12).map(m => {
        const goalMark = monthlyGoal > 0
            ? (m.amount >= monthlyGoal ? ' <i class="fas fa-circle-check positive" title="Goal met"></i>' : '')
            : '';
        return '<tr><td>' + escapeHtml(m.period) + '</td>' +
            '<td class="' + (m.amount >= 0 ? 'positive' : 'negative') + '">' + fmtMoney(m.amount, true) + goalMark + '</td></tr>';
    }).join("");

    const bySymbol = (data.by_symbol || []).map(s =>
        '<tr><td>' + escapeHtml(s.symbol) + '</td>' +
        '<td class="' + (s.premium >= 0 ? 'positive' : 'negative') + '">' + fmtMoney(s.premium, true) + '</td></tr>'
    ).join("");

    const assignments = (data.assignments || []).slice(-15).reverse().map(a =>
        '<tr><td>' + fmtDate(a.ts) + '</td><td>' + escapeHtml(a.symbol) + '</td>' +
        '<td>' + escapeHtml(a.event_type === 'PUT_ASSIGNED' ? 'Put assigned (shares in)' : 'Called away (shares out)') + '</td>' +
        '<td>' + fmtMoney(a.amount, true) + '</td></tr>'
    ).join("");

    container.innerHTML = cards +
        section('Weekly premium trend', 'fa-chart-column', trendChart || '<div class="no-results">No premium recorded yet.</div>', true) +
        outcomesSection(data.outcomes) +
        '<div class="sections-container">' +
        section('Premium by symbol', 'fa-tags',
            table(['Symbol', 'Premium'], bySymbol, 'No premium recorded yet.')) +
        section('Premium by week', 'fa-calendar-week',
            table(['Week', 'Premium'], weekRows, 'No premium recorded yet.')) +
        section('Premium by month', 'fa-calendar',
            table(['Month', 'Premium'], monthRows, 'No premium recorded yet.')) +
        section('Recent assignments', 'fa-right-left',
            table(['Date', 'Symbol', 'What happened', 'Amount'], assignments,
                  'No assignments in the imported history.')) +
        '</div>';
}

function incomeCard(label, amount, goal, streak) {
    let goalHtml = '';
    if (goal > 0 && amount !== null && amount !== undefined) {
        const pct = Math.min(100, Math.max(0, Math.round(100 * amount / goal)));
        goalHtml = '<div class="goal-bar"><div class="goal-bar-fill" style="width:' + pct + '%"></div></div>' +
            '<span class="form-hint">' + pct + '% of $' + goal.toFixed(0) + ' goal' +
            (streak ? ' — ' + streak + ' in a row' : '') + '</span>';
    }
    const known = amount !== null && amount !== undefined;
    const cls = !known ? '' : (amount >= 0 ? 'positive' : 'negative');
    return '<div class="income-card"><span class="label">' + escapeHtml(label) + '</span>' +
        '<span class="value ' + cls + '">' + (known ? fmtMoney(amount, true) : '—') + '</span>' + goalHtml + '</div>';
}

// Outcome breakdown: how sold options actually resolved. A quick health
// signal for a wheel strategy -- mostly expiring worthless is the "working
// as intended" case; heavy buyback or assignment volume is worth noticing.
function outcomesSection(outcomes) {
    if (!outcomes) return '';
    const defs = [
        { key: 'expired', label: 'Expired worthless', icon: 'fa-hourglass-end', cls: 'positive' },
        { key: 'bought_back', label: 'Bought back early', icon: 'fa-rotate-left', cls: 'negative' },
        { key: 'assigned', label: 'Assigned', icon: 'fa-right-left', cls: '' },
    ];
    const total = defs.reduce((sum, d) => sum + (outcomes[d.key]?.count || 0), 0);
    if (total === 0) return '';
    const chips = defs.map(d => {
        const o = outcomes[d.key] || { count: 0, amount: 0 };
        return '<div class="outcome-chip">' +
            '<i class="fas ' + d.icon + '"></i>' +
            '<span class="outcome-count">' + o.count + '</span>' +
            '<span class="outcome-label">' + d.label + '</span>' +
            '<span class="outcome-amount ' + (o.amount >= 0 ? 'positive' : 'negative') + '">' + fmtMoney(o.amount, true) + '</span>' +
            '</div>';
    }).join("");
    return '<section class="section"><div class="section-header static">' +
        '<h2><i class="fas fa-list-check"></i> How options resolved</h2></div>' +
        '<div class="section-content"><div class="outcomes-row">' + chips + '</div></div></section>';
}

// Small inline SVG bar chart: 12 most recent weeks, oldest-to-newest,
// diverging around a zero baseline, colored by the app's existing
// positive/negative tokens (not a new palette). Hover reveals the exact
// week + amount via a native <title> per bar.
function renderTrendChart(weekly, goal) {
    const weeks = weekly.slice(0, 12).slice().reverse();
    if (weeks.length === 0) return '';

    const width = 720, height = 160, padTop = 12, padBottom = 6, padX = 4;
    const plotH = height - padTop - padBottom;
    const baseline = padTop + plotH / 2;
    const maxAbs = Math.max(1, goal || 0, ...weeks.map(w => Math.abs(w.amount)));
    const barW = (width - padX * 2) / weeks.length;
    const barGap = Math.min(10, barW * 0.3);

    let bars = "";
    weeks.forEach((w, idx) => {
        const x = padX + idx * barW + barGap / 2;
        const w2 = Math.max(1, barW - barGap);
        const h = Math.max(1, Math.abs(w.amount) / maxAbs * (plotH / 2));
        const y = w.amount >= 0 ? baseline - h : baseline;
        const cls = w.amount >= 0 ? "positive" : "negative";
        bars += '<rect class="trend-bar ' + cls + '" x="' + x.toFixed(1) + '" y="' + y.toFixed(1) +
            '" width="' + w2.toFixed(1) + '" height="' + h.toFixed(1) + '" rx="2">' +
            '<title>' + escapeHtml(w.period) + ': ' + fmtMoney(w.amount, true) + '</title>' +
            '</rect>';
    });

    let goalLine = "";
    if (goal > 0) {
        const gy = baseline - (goal / maxAbs) * (plotH / 2);
        goalLine = '<line class="trend-goal-line" x1="' + padX + '" y1="' + gy.toFixed(1) +
            '" x2="' + (width - padX) + '" y2="' + gy.toFixed(1) + '">' +
            '<title>Weekly goal: ' + fmtMoney(goal) + '</title></line>';
    }

    const labels = weeks.map(w => '<span>' + escapeHtml(w.period.replace(/^\d+-/, '')) + '</span>').join("");

    return '<div class="trend-chart-wrap">' +
        '<svg class="trend-chart" viewBox="0 0 ' + width + ' ' + height + '" preserveAspectRatio="none">' +
        '<line class="trend-baseline" x1="0" y1="' + baseline.toFixed(1) + '" x2="' + width + '" y2="' + baseline.toFixed(1) + '"></line>' +
        goalLine + bars +
        '</svg>' +
        '<div class="trend-chart-labels">' + labels + '</div>' +
        '</div>';
}

function section(title, icon, body, noTableWrapper) {
    return '<section class="section"><div class="section-header static">' +
        '<h2><i class="fas ' + icon + '"></i> ' + escapeHtml(title) + '</h2></div>' +
        '<div class="section-content">' + (noTableWrapper ? body : '<div class="table-container">' + body + '</div>') + '</div></section>';
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
