/* Activity view: the platform wide audit trail. */
(function (global) {
    "use strict";

    const CP = global.CloudPulse;
    const { $, el, clear, fmt, ui, api } = CP;

    const state = {
        page: 1,
        perPage: 20,
        search: "",
        entityType: ""
    };

    const ACTION_TONE = {
        "incident.auto_opened": "danger",
        "incident.auto_resolved": "success",
        "incident.resolved": "success",
        "incident.escalated": "warning",
        "incident.deleted": "danger",
        "application.deleted": "danger",
        "application.paused": "warning",
        "application.resumed": "success",
        "deployment.rolled_back": "warning",
        "deployment.deleted": "danger"
    };

    function render(entries) {
        const container = clear($("#activityTimeline"));

        if (!entries.length) {
            container.appendChild(
                ui.emptyState("◷", "No activity recorded", "Changes across CloudPulse will appear here as they happen.")
            );
            return;
        }

        entries.forEach(function (entry) {
            const action = entry.action || "";
            const tone = ACTION_TONE[action] || "info";

            container.appendChild(
                el("div", { class: "timeline-item" }, [
                    el("div", { class: "row", style: "gap:8px" }, [
                        ui.badge(CP.fmt.titleCase(action.replace(/[._]/g, " ")), tone),
                        el("strong", { text: entry.summary || action })
                    ]),
                    el("p", {
                        text:
                            (entry.actor_name || "system") +
                            (entry.entity_type ? " · " + CP.fmt.titleCase(entry.entity_type) : "") +
                            (entry.entity_id ? " #" + entry.entity_id : "")
                    }),
                    el("time", { text: fmt.dateTime(entry.created_at) })
                ])
            );
        });
    }

    function load() {
        const params = {
            page: state.page,
            per_page: state.perPage
        };
        if (state.search) { params.search = state.search; }
        if (state.entityType) { params.entity_type = state.entityType; }

        ui.setLoading($("#activityTimeline"), true);

        return api.get("/api/activity", params)
            .then(function (result) {
                const entries = Array.isArray(result) ? result : result.data || [];
                render(entries);
                ui.renderPagination(
                    $("#activityPagination"),
                    Array.isArray(result) ? null : result.pagination,
                    function (page) {
                        state.page = page;
                        load();
                    }
                );
            })
            .catch(function (error) {
                CP.toastError(error, "Could not load the activity trail.");
            })
            .finally(function () {
                ui.setLoading($("#activityTimeline"), false);
            });
    }

    /* ================================================================
       Alert delivery
       ================================================================ */

    const notificationState = {
        page: 1,
        perPage: 10,
        channel: ""
    };

    const CHANNEL_TONE = {
        webhook: "info",
        email: "info",
        log: "warning"
    };

    function renderNotificationSummary(payload) {
        const container = clear($("#notificationSummary"));
        if (!container) { return; }

        const byStatus = payload.by_status || {};
        const config = payload.configuration || {};
        const rate = payload.success_rate;

        const tiles = [
            {
                label: "Delivery rate",
                value: rate === null || rate === undefined ? "—" : rate + "%",
                hint: "Sent / attempted"
            },
            {
                label: "Sent",
                value: fmt.number(byStatus.sent || 0),
                hint: "Delivered successfully"
            },
            {
                label: "Failed",
                value: fmt.number(byStatus.failed || 0),
                hint: "Retried manually"
            },
            {
                label: "Webhook",
                value: config.webhook_configured ? "Active" : "Off",
                hint: config.webhook_configured
                    ? "Incidents notify a webhook"
                    : "Set NOTIFY_WEBHOOK_URL"
            },
            {
                label: "Email",
                value: config.smtp_configured ? "Active" : "Off",
                hint: config.smtp_configured
                    ? "SMTP server configured"
                    : "Set NOTIFY_SMTP_HOST"
            }
        ];

        tiles.forEach(function (tile) {
            container.appendChild(
                el("div", { class: "stat" }, [
                    el("div", { class: "stat-label", text: tile.label }),
                    el("div", { class: "stat-value", text: String(tile.value) }),
                    el("div", { class: "stat-hint", text: tile.hint })
                ])
            );
        });
    }

    function renderNotifications(rows) {
        const body = clear($("#notificationTableBody"));
        if (!body) { return; }

        if (!rows.length) {
            body.appendChild(
                el("tr", {}, [
                    el("td", { colspan: "6" }, [
                        ui.emptyState(
                            "✉",
                            "No notifications recorded",
                            "Alert delivery attempts appear here once an incident opens or a deployment is recorded."
                        )
                    ])
                ])
            );
            return;
        }

        rows.forEach(function (row) {
            const canRetry = row.status === "failed" || row.status === "pending";

            body.appendChild(
                el("tr", {}, [
                    el("td", {}, [
                        el("span", { class: "cell-primary", text: row.event || "—" })
                    ]),
                    el("td", {}, [
                        ui.badge(
                            CP.fmt.titleCase(row.channel || "log"),
                            CHANNEL_TONE[row.channel] || "info"
                        )
                    ]),
                    el("td", {}, [
                        ui.badge(
                            CP.fmt.titleCase(row.status || "unknown"),
                            row.status === "sent" ? "success"
                                : row.status === "failed" ? "danger"
                                : row.status === "pending" ? "warning"
                                : "info"
                        )
                    ]),
                    el("td", { class: "numeric", text: String(row.attempts || 0) }),
                    el("td", {
                        class: "cell-muted",
                        text: fmt.dateTime(row.created_at)
                    }),
                    el("td", { class: "numeric" }, [
                        canRetry
                            ? el("button", {
                                type: "button",
                                class: "btn btn-sm",
                                text: "Retry",
                                "data-retry-notification": String(row.id)
                            })
                            : el("span", { class: "cell-muted", text: "—" })
                    ])
                ])
            );
        });
    }

    function loadNotifications() {
        const params = {
            page: notificationState.page,
            per_page: notificationState.perPage
        };
        if (notificationState.channel) {
            params.channel = notificationState.channel;
        }

        return Promise.all([
            api.get("/api/activity/notifications", params),
            api.get("/api/activity/notifications/summary")
        ])
            .then(function (results) {
                const payload = results[0];
                const rows = Array.isArray(payload) ? payload : payload.data || [];

                renderNotifications(rows);
                renderNotificationSummary(results[1] || {});

                ui.renderPagination(
                    $("#notificationPagination"),
                    Array.isArray(payload) ? null : payload.pagination,
                    function (page) {
                        notificationState.page = page;
                        loadNotifications();
                    }
                );
            })
            .catch(function (error) {
                CP.toastError(error, "Could not load the notification outbox.");
            });
    }

    function retryNotification(id) {
        return api.post("/api/activity/notifications/" + id + "/retry", {})
            .then(function () {
                CP.toast({ type: "success", title: "Notification retried" });
                return loadNotifications();
            })
            .catch(function (error) {
                CP.toastError(error, "Could not retry that notification.");
            });
    }

    /* ================================================================
       Stored history and retention
       ================================================================ */

    const RETENTION_LABEL = {
        health_checks: "Health checks",
        audit_logs: "Audit log",
        notifications: "Notifications",
        auth_tokens: "Auth tokens"
    };

    function renderRetention(payload) {
        const container = clear($("#retentionSummary"));
        if (!container) { return; }

        const counts = payload.counts || {};

        Object.keys(RETENTION_LABEL).forEach(function (key) {
            container.appendChild(
                el("div", { class: "stat" }, [
                    el("div", { class: "stat-label", text: RETENTION_LABEL[key] }),
                    el("div", {
                        class: "stat-value",
                        text: fmt.number(counts[key] || 0)
                    })
                ])
            );
        });

        const note = $("#retentionPolicyNote");
        if (note) {
            const policies = payload.policies || {};
            note.textContent =
                "The scheduled retention job runs every " +
                (policies.job_interval_hours || "?") +
                " hours and deletes health checks older than " +
                (policies.health_checks_days || "?") +
                " days, audit entries older than " +
                (policies.audit_logs_days || "?") +
                " days and notifications older than " +
                (policies.notifications_days || "?") +
                " days. Set a window to 0 to keep that table forever.";
        }
    }

    function loadRetention() {
        return api.get("/api/monitor/retention")
            .then(renderRetention)
            .catch(function (error) {
                CP.toastError(error, "Could not load the retention summary.");
            });
    }

    function pruneRetention() {
        return ui.confirm({
            title: "Prune stored history?",
            message:
                "Rows older than their configured retention window are deleted " +
                "permanently. This cannot be undone.",
            confirmLabel: "Prune",
            danger: true
        }).then(function (confirmed) {
            if (!confirmed) { return null; }

            return api.post("/api/monitor/retention", {})
                .then(function (result) {
                    const removed = (result && result.removed) || 0;
                    CP.toast({ type: "success", title: "Pruned " + removed + " records" });
                    return loadRetention();
                })
                .catch(function (error) {
                    CP.toastError(error, "Could not run the retention job.");
                });
        });
    }

    function init() {
        const search = $("#activitySearch");
        if (search) {
            search.addEventListener(
                "input",
                ui.debounce(function () {
                    state.search = search.value.trim();
                    state.page = 1;
                    load();
                }, 280)
            );
        }

        const typeFilter = $("#activityTypeFilter");
        if (typeFilter) {
            typeFilter.addEventListener("change", function () {
                state.entityType = typeFilter.value;
                state.page = 1;
                load();
            });
        }

        CP.on("click", "#activityExportBtn", function () {
            api.download("/api/export/activity");
        });

        CP.on("click", "#notificationRefreshBtn", function () {
            loadNotifications();
        });

        CP.on("change", "#notificationChannelFilter", function (event) {
            notificationState.channel = event.target.value;
            notificationState.page = 1;
            loadNotifications();
        });

        // Delegated so it keeps working after the table body is re-rendered.
        CP.on("click", "[data-retry-notification]", function (event) {
            retryNotification(event.currentTarget.dataset.retryNotification);
        });

        CP.on("click", "#retentionPruneBtn", function () {
            pruneRetention();
        });
    }

    function loadAll() {
        return Promise.all([load(), loadNotifications(), loadRetention()]);
    }

    global.CloudPulse.views = global.CloudPulse.views || {};
    global.CloudPulse.views.activity = { init: init, load: loadAll };
})(window);
