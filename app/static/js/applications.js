/* Applications view: registry table, filters, forms and health history. */
(function (global) {
    "use strict";

    const CP = global.CloudPulse;
    const { $, $$, el, clear, fmt, ui, api } = CP;

    const state = {
        page: 1,
        perPage: 10,
        sort: "id",
        order: "desc",
        search: "",
        environment: "",
        health: "",
        pausedOnly: false,
        selected: new Set(),
        editing: null,
        historyId: null
    };

    /* ------------------------------------------------------ rendering */

    function actionButton(label, title, className, handler) {
        return el("button", {
            type: "button",
            class: "btn btn-sm " + (className || "btn-ghost"),
            title: title,
            text: label,
            onClick: handler
        });
    }

    function renderRow(application) {
        const checkbox = el("input", {
            type: "checkbox",
            "aria-label": "Select " + application.name,
            checked: state.selected.has(application.id),
            onChange: function (event) {
                if (event.target.checked) {
                    state.selected.add(application.id);
                } else {
                    state.selected.delete(application.id);
                }
                updateSelectionUi();
            }
        });

        const healthLabel = application.last_health_status || "UNKNOWN";

        return el("tr", { dataset: { id: application.id } }, [
            el("td", null, checkbox),
            el("td", null, [
                el("div", { class: "cell-primary", text: application.name }),
                el("div", { class: "cell-muted truncate", text: application.url }),
                application.description
                    ? el("div", { class: "cell-muted truncate", text: application.description })
                    : null
            ]),
            el("td", null, ui.environmentBadge(application.environment)),
            el("td", null, [
                ui.badge(application.status, application.status),
                application.is_paused ? el("div", { class: "mt-16" }, ui.badge("Paused", "low")) : null
            ]),
            el("td", null, [
                ui.statusBadge(healthLabel),
                application.consecutive_failures
                    ? el("div", { class: "cell-muted", text: application.consecutive_failures + " failed in a row" })
                    : null
            ]),
            el("td", { class: "numeric", text: fmt.ms(application.last_response_time) }),
            el("td", { class: "numeric" }, [
                el("div", { text: fmt.percent(application.uptime_percentage, 1) }),
                ui.renderMeter(application.uptime_percentage, ui.uptimeVariant(application.uptime_percentage))
            ]),
            el("td", { class: "numeric" },
                application.open_incident_count
                    ? el("span", { class: "badge badge-open", text: String(application.open_incident_count) })
                    : el("span", { class: "subtle", text: "0" })
            ),
            el("td", { text: fmt.relativeTime(application.last_checked_at) }),
            el("td", { class: "text-right" }, el("div", { class: "btn-group", style: "justify-content:flex-end" }, [
                actionButton("Check", "Run a health check now", "btn-ghost", function () {
                    runCheck(application);
                }),
                actionButton("History", "Open health history", "btn-ghost", function () {
                    openHistory(application);
                }),
                CP.store.canWrite ? actionButton("Edit", "Edit application", "", function () {
                    openForm(application);
                }) : null,
                CP.store.canWrite
                    ? actionButton(application.is_paused ? "Resume" : "Pause",
                        application.is_paused ? "Resume monitoring" : "Pause monitoring",
                        "btn-ghost", function () {
                            togglePause(application);
                        })
                    : null,
                CP.store.canWrite
                    ? actionButton("Delete", "Delete application", "btn-danger", function () {
                        remove(application);
                    })
                    : null
            ]))
        ]);
    }

    function render(payload, pagination) {
        const body = clear($("#applicationsBody"));
        const rows = Array.isArray(payload) ? payload : payload.data || [];

        if (!rows.length) {
            body.appendChild(
                el("tr", null,
                    el("td", { colspan: "10" },
                        ui.emptyState(
                            "◍",
                            "No applications match your filters",
                            "Adjust the filters, or register a new application to start monitoring it.",
                            CP.store.canWrite
                                ? el("button", {
                                    type: "button",
                                    class: "btn btn-primary mt-16",
                                    text: "+ Add application",
                                    onClick: function () { openForm(null); }
                                })
                                : null
                        )
                    )
                )
            );
        } else {
            rows.forEach(function (application) {
                body.appendChild(renderRow(application));
            });
        }

        ui.renderPagination($("#appPagination"), pagination, function (page) {
            state.page = page;
            load();
        });

        const selectAll = $("#appSelectAll");
        if (selectAll) {
            selectAll.checked =
                rows.length > 0 && rows.every(function (row) { return state.selected.has(row.id); });
        }
    }

    function renderStats(overview) {
        if (!overview) {
            return;
        }
        const set = function (name, value) {
            const node = document.querySelector('#appStats [data-stat="' + name + '"]');
            if (node) {
                node.textContent = value;
            }
        };

        set("total", fmt.number(overview.applications.total));
        set("healthy", fmt.number(overview.applications.healthy));
        set("degraded", fmt.number(overview.applications.degraded));
        set("down", fmt.number(overview.applications.unhealthy));
        set("latency", fmt.ms(overview.monitoring && overview.monitoring.avg_response_time_ms));
    }

    function updateSelectionUi() {
        const count = state.selected.size;
        const hint = $("#appSelectionHint");
        const pause = $("#appBulkPause");
        const remove = $("#appBulkDelete");

        if (hint) {
            hint.textContent = count
                ? count + " selected"
                : "No rows selected";
        }
        if (pause) {
            pause.classList.toggle("hidden", count === 0);
        }
        if (remove) {
            remove.classList.toggle("hidden", count === 0);
        }
    }

    /* --------------------------------------------------------- loading */

    function load() {
        const tbody = $("#applicationsBody");
        ui.setLoading(tbody.parentElement, true);

        const params = {
            page: state.page,
            per_page: state.perPage,
            sort: state.sort,
            order: state.order
        };

        if (state.search) { params.search = state.search; }
        if (state.environment) { params.environment = state.environment; }
        if (state.health) { params.health = state.health; }
        if (state.pausedOnly) { params.is_paused = "true"; }

        return Promise.all([
            api.get("/api/applications", params),
            api.get("/api/monitor/summary")
        ])
            .then(function (results) {
                render(results[0], Array.isArray(results[0]) ? null : results[0].pagination);
                renderStats({
                    applications: {
                        total: results[1].total_applications,
                        healthy: results[1].healthy,
                        degraded: results[1].degraded,
                        unhealthy: results[1].unhealthy
                    },
                    monitoring: { avg_response_time_ms: results[1].avg_response_time_ms }
                });
            })
            .catch(function (error) {
                CP.toastError(error, "Could not load applications.");
            })
            .finally(function () {
                ui.setLoading(tbody.parentElement, false);
            });
    }

    /* ----------------------------------------------------------- forms */

    function fillSelect(select, values, selected) {
        if (!select) {
            return;
        }
        clear(select);
        values.forEach(function (value) {
            const option = el("option", { value: value, text: CP.fmt.titleCase(value) });
            if (value === selected) {
                option.selected = true;
            }
            select.appendChild(option);
        });
    }

    function openForm(application) {
        if (!CP.store.canWrite) {
            CP.toast({
                type: "warning",
                title: "Read only",
                message: "Your role cannot modify applications."
            });
            return;
        }

        state.editing = application || null;

        $("#appFormTitle").textContent = application ? "Edit application" : "Add application";
        $("#appSubmitBtn").textContent = application ? "Save changes" : "Save application";
        $("#appIdField").value = application ? application.id : "";

        $("#appName").value = application ? application.name : "";
        $("#appUrl").value = application ? application.url : "";
        $("#appDescription").value = application && application.description ? application.description : "";

        fillSelect($("#appEnvironment"), CP.store.metadata.applications.environments || [],
            application ? application.environment : "Production");
        fillSelect($("#appStatus"), CP.store.metadata.applications.statuses || [],
            application ? application.status : "Operational");
        fillSelect($("#appMethod"), CP.store.metadata.applications.http_methods || [],
            (application && application.monitoring && application.monitoring.http_method) || "GET");

        const monitoring = (application && application.monitoring) || {};
        $("#appTimeout").value = monitoring.timeout_seconds || 5;
        $("#appInterval").value = monitoring.check_interval_seconds || 60;
        $("#appExpected").value = monitoring.expected_status_codes || "200-399";
        $("#appKeyword").value = monitoring.expected_body_keyword || "";

        let headers = "";
        if (application && application.monitoring && application.monitoring.request_headers) {
            try {
                headers = JSON.stringify(
                    JSON.parse(application.monitoring.request_headers), null, 2
                );
            } catch (error) {
                headers = "";
            }
        }
        if (!headers) {
            const existing = $("#appHeaders").value;
            headers = existing && existing !== "{}" ? existing : "";
        }
        $("#appHeaders").value = headers;

        // When editing, the advanced block is opened so the current monitoring
        // configuration is visible instead of hidden behind a summary.
        const advanced = $("#appAdvancedFields");
        if (advanced) {
            advanced.open = !!application;
        }

        CP.modal.open("appFormModal");
    }

    function collectForm() {
        const rawHeaders = $("#appHeaders").value.trim();
        let headers = null;

        if (rawHeaders) {
            try {
                headers = JSON.parse(rawHeaders);
            } catch (error) {
                CP.toast({
                    type: "error",
                    title: "Invalid headers",
                    message: "Custom request headers must be valid JSON."
                });
                return null;
            }
        }

        return {
            name: $("#appName").value.trim(),
            url: $("#appUrl").value.trim(),
            environment: $("#appEnvironment").value,
            status: $("#appStatus").value,
            description: $("#appDescription").value.trim(),
            http_method: $("#appMethod").value,
            timeout_seconds: Number($("#appTimeout").value) || 5,
            check_interval_seconds: Number($("#appInterval").value) || 60,
            expected_status_codes: $("#appExpected").value.trim() || "200-399",
            expected_body_keyword: $("#appKeyword").value.trim() || null,
            request_headers: headers
        };
    }

    function submitForm(event) {
        event.preventDefault();

        const payload = collectForm();
        if (!payload) {
            return;
        }

        if (!payload.name || !payload.url) {
            CP.toast({
                type: "warning",
                title: "Missing fields",
                message: "Name and URL are required."
            });
            return;
        }

        const button = $("#appSubmitBtn");
        ui.setLoading(button, true);
        button.textContent = "Saving…";

        const editing = state.editing;
        const request = editing
            ? api.put("/api/applications/" + editing.id, payload)
            : api.post("/api/applications", payload);

        request
            .then(function (result) {
                CP.modal.close("appFormModal");
                CP.toast({
                    type: "success",
                    title: editing ? "Application updated" : "Application created",
                    message: result.message
                });
                state.page = 1;
                return load();
            })
            .catch(function (error) {
                CP.toastError(error, "Could not save the application.");
            })
            .finally(function () {
                ui.setLoading(button, false);
                button.textContent = editing ? "Save changes" : "Save application";
            });
    }

    /* ---------------------------------------------------------- actions */

    function runCheck(application) {
        api.post("/api/applications/" + application.id + "/check")
            .then(function (result) {
                const tone = result.health === "UP" ? "success" : result.health === "DEGRADED" ? "warning" : "error";
                CP.toast({
                    type: tone,
                    title: application.name + " is " + result.health,
                    message: result.message + " · " + fmt.ms(result.response_time_ms)
                });
                return load();
            })
            .catch(function (error) {
                CP.toastError(error, "Health check failed to run.");
            });
    }

    function togglePause(application) {
        const action = application.is_paused ? "resume" : "pause";
        api.post("/api/applications/" + application.id + "/" + action)
            .then(function (result) {
                CP.toast({ type: "success", title: result.message, message: application.name });
                return load();
            })
            .catch(function (error) {
                CP.toastError(error);
            });
    }

    function remove(application) {
        CP.modal
            .confirm({
                title: "Delete " + application.name + "?",
                message:
                    "This permanently removes the application together with its health history, incidents and deployments.",
                confirmLabel: "Delete application"
            })
            .then(function (confirmed) {
                if (!confirmed) {
                    return null;
                }
                return api.del("/api/applications/" + application.id).then(function (result) {
                    state.selected.delete(application.id);
                    CP.toast({ type: "success", title: result.message, message: application.name });
                    return load();
                });
            })
            .catch(function (error) {
                CP.toastError(error, "Could not delete the application.");
            });
    }

    function bulkPause() {
        const ids = Array.from(state.selected);
        if (!ids.length) {
            return;
        }
        Promise.all(ids.map(function (id) { return api.post("/api/applications/" + id + "/pause"); }))
            .then(function () {
                CP.toast({ type: "success", title: ids.length + " applications paused" });
                state.selected.clear();
                updateSelectionUi();
                return load();
            })
            .catch(function (error) {
                CP.toastError(error);
            });
    }

    function bulkDelete() {
        const ids = Array.from(state.selected);
        if (!ids.length) {
            return;
        }
        CP.modal
            .confirm({
                title: "Delete " + ids.length + " applications?",
                message: "All related incidents, deployments and health checks will be removed.",
                confirmLabel: "Delete all"
            })
            .then(function (confirmed) {
                if (!confirmed) {
                    return null;
                }
                return Promise.all(ids.map(function (id) { return api.del("/api/applications/" + id); }))
                    .then(function () {
                        CP.toast({ type: "success", title: ids.length + " applications deleted" });
                        state.selected.clear();
                        updateSelectionUi();
                        return load();
                    });
            })
            .catch(function (error) {
                CP.toastError(error);
            });
    }

    function monitorAll() {
        const button = $("#appMonitorBtn");
        ui.setLoading(button, true);
        button.textContent = "Checking…";

        api.post("/api/monitor/run")
            .then(function (result) {
                const summary = result.summary;
                CP.toast({
                    type: summary.unhealthy ? "warning" : "success",
                    title: "Monitoring cycle finished",
                    message:
                        summary.total + " checked · " + summary.healthy + " up · " +
                        summary.degraded + " degraded · " + summary.unhealthy + " down"
                });
                return load();
            })
            .catch(function (error) {
                CP.toastError(error, "Monitoring cycle failed.");
            })
            .finally(function () {
                ui.setLoading(button, false);
                button.textContent = "Run all checks";
            });
    }

    /* ------------------------------------------------- health history */

    function openHistory(application) {
        $("#healthHistoryTitle").textContent = application.name;
        $("#healthHistorySubtitle").textContent = application.url;
        state.historyId = application.id;
        clear($("#historyBody"));
        CP.modal.open("healthHistoryModal");
        loadHistory(application.id);
    }

    function loadHistory(applicationId) {
        const hours = Number($("#historyRange").value) || 6;

        Promise.all([
            api.get("/api/applications/" + applicationId + "/health-history", {
                limit: 100,
                hours: hours
            }),
            api.get("/api/applications/" + applicationId + "/uptime", { days: 1 })
        ])
            .then(function (results) {
                renderHistoryStats(results[0].summary);
                renderHistorySparkline(results[1].series || []);
                renderHistoryRows(results[0].history || []);
            })
            .catch(function (error) {
                CP.toastError(error, "Could not load health history.");
            });
    }

    function renderHistoryStats(summary) {
        const host = clear($("#historyStats"));
        const cards = [
            { label: "Checks", value: fmt.number(summary.total_checks), variant: "" },
            { label: "Healthy", value: fmt.number(summary.up_checks), variant: "is-success" },
            { label: "Failed", value: fmt.number(summary.down_checks), variant: "is-danger" },
            {
                label: "Uptime",
                value: fmt.percent(summary.uptime_percentage, 2),
                variant: summary.uptime_percentage >= 99 ? "is-success" : summary.uptime_percentage >= 95 ? "is-warning" : "is-danger"
            },
            { label: "Avg latency", value: fmt.ms(summary.avg_response_time), variant: "is-info" }
        ];

        cards.forEach(function (card) {
            host.appendChild(
                el("div", { class: "stat " + card.variant }, el("div", { class: "stat-body" }, [
                    el("div", { class: "stat-label", text: card.label }),
                    el("div", { class: "stat-value", text: card.value })
                ]))
            );
        });
    }

    function renderHistorySparkline(series) {
        const values = series
            .map(function (point) { return point.avg_response_time; })
            .filter(function (value) { return value !== null && value !== undefined; });

        CP.charts.line($("#historySparkline"), {
            labels: series.map(function (point) { return point.date; }),
            height: 150,
            series: [
                {
                    name: "Average response time (ms)",
                    values: values,
                    color: CP.charts.cssVar("--accent", "#6c8bff")
                }
            ],
            formatLabel: function (label) {
                return new Date(label).toLocaleDateString(undefined, { month: "short", day: "numeric" });
            },
            emptyMessage: "Not enough data for a trend line yet"
        });
    }

    function renderHistoryRows(history) {
        const body = clear($("#historyBody"));

        if (!history.length) {
            body.appendChild(
                el("tr", null,
                    el("td", { colspan: "5" },
                        ui.emptyState("◷", "No health checks recorded", "Run a check to start building history.")
                    )
                )
            );
            return;
        }

        history.forEach(function (check) {
            body.appendChild(
                el("tr", null, [
                    el("td", { class: "nowrap", text: fmt.dateTime(check.checked_at) }),
                    el("td", null, ui.statusBadge(check.status)),
                    el("td", { class: "numeric", text: check.http_status === null ? "—" : check.http_status }),
                    el("td", { class: "numeric", text: fmt.ms(check.response_time) }),
                    el("td", { class: "cell-muted truncate", text: check.error_message || "OK" })
                ])
            );
        });
    }

    /* ------------------------------------------------------------ init */

    function init() {
        const form = $("#appForm");
        if (form) {
            form.addEventListener("submit", submitForm);
        }

        CP.on("click", "#appCreateBtn", function () { openForm(null); });
        CP.on("click", "#appMonitorBtn", monitorAll);
        CP.on("click", "#appBulkPause", bulkPause);
        CP.on("click", "#appBulkDelete", bulkDelete);
        CP.on("click", "#appExportBtn", function () {
            api.download("/api/export/applications");
        });

        const selectAll = $("#appSelectAll");
        if (selectAll) {
            selectAll.addEventListener("change", function () {
                const checked = selectAll.checked;
                $$("#applicationsBody input[type=checkbox]").forEach(function (checkbox) {
                    checkbox.checked = checked;
                    const row = checkbox.closest("tr");
                    const id = Number(row.dataset.id);
                    if (checked) {
                        state.selected.add(id);
                    } else {
                        state.selected.delete(id);
                    }
                });
                updateSelectionUi();
            });
        }

        const search = $("#appSearch");
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

        [["#appEnvFilter", "environment"], ["#appStatusFilter", "health"]].forEach(function (pair) {
            const node = $(pair[0]);
            if (node) {
                node.addEventListener("change", function () {
                    state[pair[1]] = node.value;
                    state.page = 1;
                    load();
                });
            }
        });

        const paused = $("#appPausedFilter");
        if (paused) {
            paused.addEventListener("change", function () {
                state.pausedOnly = paused.checked;
                state.page = 1;
                load();
            });
        }

        $$("#view-applications th.sortable").forEach(function (header) {
            header.addEventListener("click", function () {
                const column = header.dataset.sort;
                if (state.sort === column) {
                    state.order = state.order === "asc" ? "desc" : "asc";
                } else {
                    state.sort = column;
                    state.order = "asc";
                }
                state.page = 1;
                load();
            });
        });

        const range = $("#historyRange");
        if (range) {
            range.addEventListener("change", function () {
                const open = $("#healthHistoryModal").classList.contains("is-open");
                if (!open) {
                    return;
                }
                if (state.historyId) {
                    loadHistory(state.historyId);
                }
            });
        }

        updateSelectionUi();
    }

    function primeEnvironmentFilter() {
        const select = $("#appEnvFilter");
        if (!select || select.options.length > 1) {
            return;
        }
        (CP.store.metadata.applications.environments || []).forEach(function (value) {
            select.appendChild(el("option", { value: value, text: CP.fmt.titleCase(value) }));
        });
    }

    global.CloudPulse.views = global.CloudPulse.views || {};
    global.CloudPulse.views.applications = {
        init: init,
        load: load,
        prime: primeEnvironmentFilter
    };
})(window);
