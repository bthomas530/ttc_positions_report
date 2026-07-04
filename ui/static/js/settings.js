// Settings tab: Flex Query setup, trading preferences, data export

async function loadSettings() {
    try {
        const response = await fetch("/api/settings");
        const data = await response.json();

        document.getElementById("flexQueryId").value = data.flex_query_id || "";
        document.getElementById("flexToken").value = "";
        document.getElementById("flexTokenHint").textContent = data.flex_token_set
            ? "A token is saved (" + data.flex_token_masked + "). Paste a new one only to replace it."
            : "No token saved yet.";
        document.getElementById("buybackThreshold").value = data.buyback_threshold_pct;
        document.getElementById("weeklyGoal").value = data.weekly_premium_goal || 0;
        document.getElementById("monthlyGoal").value = data.monthly_premium_goal || 0;

        const info = document.getElementById("dataInfo");
        const last = data.last_import;
        info.innerHTML =
            'Trades imported: <b>' + data.trade_count + '</b><br>' +
            'Last import: ' + (last
                ? escapeHtml((last.requested_ts || '').replace('T', ' ').slice(0, 16)) +
                  ' — ' + (last.status === 'ok'
                      ? (last.new_count + ' new of ' + last.trade_count)
                      : 'failed: ' + escapeHtml(last.error || ''))
                : 'never') + '<br>' +
            'Database: ' + escapeHtml(data.data_dir);
    } catch (err) {
        showToast("Could not load settings: " + err.message, "error");
    }
}

async function saveSettings(fields, message) {
    try {
        const response = await fetch("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(fields),
        });
        const result = await response.json();
        if (!result.success) throw new Error(result.error || "Save failed");
        showToast(message, "success");
        loadSettings();
    } catch (err) {
        showToast("Save failed: " + err.message, "error");
    }
}

let importPollTimer = null;

async function pollImportStatus() {
    const statusEl = document.getElementById("importStatus");
    try {
        const response = await fetch("/api/flex/status");
        const data = await response.json();
        if (data.running) {
            statusEl.className = "import-status";
            statusEl.innerHTML = '<i class="fas fa-circle-notch fa-spin"></i> Importing from IBKR… (this can take up to a minute)';
            importPollTimer = setTimeout(pollImportStatus, 2000);
            return;
        }
        document.getElementById("importNowBtn").disabled = false;
        const result = data.result;
        if (result && result.ok) {
            statusEl.className = "import-status ok";
            statusEl.innerHTML = '<i class="fas fa-circle-check"></i> Imported ' +
                result.trade_count + ' trades (' + result.new_count + ' new).';
            showToast("Trade history imported", "success");
            // Rebuilt tranches are ready — refresh the tabs that show them
            loadedTabs.tranches = false;
            loadedTabs.income = false;
            loadSettings();
        } else if (result) {
            statusEl.className = "import-status error";
            statusEl.innerHTML = '<i class="fas fa-circle-xmark"></i> ' + escapeHtml(result.error || "Import failed.");
        } else {
            statusEl.textContent = "";
        }
    } catch (err) {
        statusEl.className = "import-status error";
        statusEl.textContent = "Could not check import status: " + err.message;
        document.getElementById("importNowBtn").disabled = false;
    }
}

async function startImport() {
    const btn = document.getElementById("importNowBtn");
    const statusEl = document.getElementById("importStatus");
    btn.disabled = true;
    try {
        const response = await fetch("/api/flex/import", { method: "POST" });
        const result = await response.json();
        if (!result.success) {
            btn.disabled = false;
            statusEl.className = "import-status error";
            statusEl.textContent = result.error || "Could not start import.";
            return;
        }
        pollImportStatus();
    } catch (err) {
        btn.disabled = false;
        statusEl.className = "import-status error";
        statusEl.textContent = "Could not start import: " + err.message;
    }
}

async function exportData() {
    const btn = document.getElementById("exportDataBtn");
    btn.disabled = true;
    try {
        const response = await fetch("/api/export", { method: "POST" });
        const result = await response.json();
        if (result.success) {
            showToast("Exported " + result.files.length + " files to the export folder", "success", 5000);
        } else {
            showToast("Export failed: " + (result.error || ""), "error");
        }
    } catch (err) {
        showToast("Export failed: " + err.message, "error");
    } finally {
        btn.disabled = false;
    }
}

function loadNotificationSettings() {
    const notif = getNotificationPrefs();
    document.getElementById("notifEnabled").checked = notif.enabled;
    document.getElementById("notifPosition").value = notif.position;
    document.getElementById("notifDataSource").checked = notif.categories.dataSource;
    document.getElementById("notifActions").checked = notif.categories.actions;
    document.getElementById("notifErrors").checked = notif.categories.errors;
    document.getElementById("notifRefresh").checked = notif.categories.refresh;
    document.getElementById("notifSubOptions").classList.toggle("disabled", !notif.enabled);
}

document.addEventListener("DOMContentLoaded", () => {
    loadNotificationSettings();

    document.getElementById("notifEnabled").addEventListener("change", (e) => {
        saveNotificationPrefs({ enabled: e.target.checked });
        document.getElementById("notifSubOptions").classList.toggle("disabled", !e.target.checked);
        applyToastPosition(getNotificationPrefs());
    });
    document.getElementById("notifPosition").addEventListener("change", (e) => {
        saveNotificationPrefs({ position: e.target.value });
        applyToastPosition(getNotificationPrefs());
    });
    document.getElementById("notifDataSource").addEventListener("change", (e) => {
        saveNotificationPrefs({ categories: { dataSource: e.target.checked } });
    });
    document.getElementById("notifActions").addEventListener("change", (e) => {
        saveNotificationPrefs({ categories: { actions: e.target.checked } });
    });
    document.getElementById("notifErrors").addEventListener("change", (e) => {
        saveNotificationPrefs({ categories: { errors: e.target.checked } });
    });
    document.getElementById("notifRefresh").addEventListener("change", (e) => {
        saveNotificationPrefs({ categories: { refresh: e.target.checked } });
    });

    document.getElementById("saveFlexBtn").addEventListener("click", () => {
        saveSettings({
            flex_token: document.getElementById("flexToken").value.trim(),
            flex_query_id: document.getElementById("flexQueryId").value.trim(),
        }, "Flex settings saved");
    });
    document.getElementById("savePrefsBtn").addEventListener("click", () => {
        saveSettings({
            buyback_threshold_pct: parseFloat(document.getElementById("buybackThreshold").value) || 15,
            weekly_premium_goal: parseFloat(document.getElementById("weeklyGoal").value) || 0,
            monthly_premium_goal: parseFloat(document.getElementById("monthlyGoal").value) || 0,
        }, "Preferences saved");
    });
    document.getElementById("importNowBtn").addEventListener("click", startImport);
    document.getElementById("exportDataBtn").addEventListener("click", exportData);
});
