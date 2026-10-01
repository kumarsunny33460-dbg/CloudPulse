/* Incidents view: triage table, forms, detail drawer and bulk actions. */
(function (global) {
    "use strict";

    const CP = global.CloudPulse;
    const { $, $$, el, clear, fmt, ui, api } = CP;

    const state = {
        page: 1,
        perPage: 10,
        sort: "detected_at",
        order: "desc",
        search: "",
        status: "",
        severity: "",
        openOnly: true,
        breachedOnly: false,
        selected: new Set(),
        editing: null
    };

    function severityBadge(severity) {
        return ui.badge(severity, severity);
    }

    function slaCell(incident) {
        if (!incident.sla_minutes) {
            return el("span", { class: "subtle", text: "—" });
        }

        if (incident.status === "Resolved") {
            return el("span", {
                class: incident.sla_breached ? "badge badge-failed" : "badge badge-success",
                text: incident.sla_breached ? "Missed" : "Met"
            });
        }

        const variant = incident.sla_breached
            ? "badge badge-failed"
            : incident.sla_remaining_minutes < incident.sla_minutes * 0.25
                ? "badge badge-medium"
                : "badge badge-low";

        return el("span", {
            class: variant,
            title: "SLA target " + incident.sla_minutes + " minutes",
            text: incident.sla_breached
                ? "Over by " + fmt.duration(Math.abs(incident.sla_remaining_minutes))
                : fmt.duration(incident.sla_remaining_minutes) + " left"
        });
    }

    function renderRow(incident) {
        const checkbox = el("input", {
            type: "checkbox",
            "aria-label": "Select incident " + incident.id,
            checked: state.selected.has(incident.id),
            onChange: function (event) {
                if (event.target.checked) {
                    state.selected.add(incident.id);
                } else {
                    state.selected.delete(incident.id);
                }
                updateSelectionUi();
            }
        });

        const actions = [
            el("button", {
                type: "button",
                class: "btn btn-sm btn-ghost",
                text: "Details",
                onClick: function () { openDetail(incident.id); }
            })
        ];

        if (CP.store.canWrite && incident.status !== "Resolved") {
            actions.push(
                el("button", {
                    type: "button",
                    class: "btn btn-sm btn-primary",
                    text: "Resolve",
                    onClick: function () { resolve(incident); }
                })
            );
        }

        if (CP.store.canWrite && incident.severity !== "Critical" && incident.status !== "Resolved") {
            actions.push(
                el("button", {
                    type: "button",
                    class: "btn btn-sm btn-ghost",
                    text: "Escalate",
                    title: "Raise severity one level",
                    onClick: function () { escalate(incident); }
                })
            );
        }

        if (CP.store.canWrite && incident.status === "Open") {
            actions.push(
                el("button", {
                    type: "button",
                    class: "btn btn-sm btn-ghost",
                    text: "Ack",
                    title: "Acknowledge",
                    onClick: function () { acknowledge(incident); }
                })
            );
        }

        if (CP.store.canWrite) {
            actions.push(
                el("button", {
                    type: "button",
                    class: "btn btn-sm btn-danger",
                    text: "Delete",
                    onClick: function () { remove(incident); }
                })
            );
        }

        return el("tr", { dataset: { id: incident.id } }, [
            el("td", null, checkbox),
            el("td", null, severityBadge(incident.severity)),
            el("td", null, [
                el("div", { class: "cell-primary", text: incident.title }),
                el("div", { class: "cell-muted", text: incident.application_name || "Unknown application" }),
                incident.source === "monitoring"
                    ? el("div", { class: "cell-muted" }, ui.badge("Auto", "low"))
                    : null
            ]),
            el("td", null, ui.badge(incident.status, incident.status)),
            el("td", { text: incident.assignee || el("span", { class: "subtle", text: "Unassigned" }) }),
            el("td", { class: "nowrap", text: fmt.relativeTime(incident.detected_at) }),
            el("td", { class: "numeric", text: fmt.duration(incident.duration_minutes) }),
            el("td", { class: "numeric" }, slaCell(incident)),
            el("td", { class: "text-right" }, el("div", { class: "btn-group", style: "justify-content:flex-end" }, actions))
        ]);
    }

    function render(payload, pagination) {
        const body = clear($("#incidentsBody"));
        const rows = Array.isArray(payload) ? payload : payload.data || [];

        if (!rows.length) {
            body.appendChild(
                el("tr", null,
                    el("td", { colspan: "9" },
                        ui.emptyState(
                            "✓",
                            "No incidents to show",
                            state.openOnly
                                ? "Nothing is currently open. Switch off 'Open only' to review resolved incidents."
                                : "Adjust the filters or record an incident manually.",
                            CP.store.canWrite
                                ? el("button", {
                                    type: "button",
                                    class: "btn btn-primary mt-16",
                                    text: "+ New incident",
                                    onClick: function () { openForm(null); }
                                })
                                : null
                        )
                    )
                )
            );
        } else {
            rows.forEach(function (incident) {
                body.appendChild(renderRow(incident));
            });
        }

        ui.renderPagination($("#incidentPagination"), pagination, function (page) {
            state.page = page;
            load();
        });
    }

    function renderStats(stats) {
        if (!stats) {
            return;
        }
        const reliability = stats.reliability || {};
        const set = function (name, value) {
            const node = document.querySelector('#incidentStats [data-stat="' + name + '"]');
            if (node) {
                node.textContent = value;
            }
        };

        const critical = (stats.severity_breakdown || []).filter(function (row) {
            return row.severity === "Critical";
        })[0];

        set("open", fmt.number(reliability.open_incidents));
        set("critical", fmt.number(critical ? critical.open : 0));
        set("breached", fmt.number(reliability.sla_breaches));
        set("mttr", reliability.mttr_minutes ? fmt.duration(reliability.mttr_minutes) : "—");
        set("compliance", fmt.percent(reliability.sla_compliance_percentage, 1));
    }

    function updateSelectionUi() {
        const count = state.selected.size;
        const resolve = $("#incidentBulkResolve");
        const remove = $("#incidentBulkDelete");
        if (resolve) {
            resolve.classList.toggle("hidden", count === 0);
        }
        if (remove) {
            remove.classList.toggle("hidden", count === 0);
        }
    }

    /* --------------------------------------------------------- loading */

    function load() {
        const wrap = $("#incidentsBody").parentElement;
        ui.setLoading(wrap, true);

        const params = {
            page: state.page,
            per_page: state.perPage,
            sort: state.sort,
            order: state.order
        };

        if (state.search) { params.search = state.search; }
        if (state.status) { params.status = state.status; }
        if (state.severity) { params.severity = state.severity; }
        if (state.openOnly) { params.open = "true"; }
        if (state.breachedOnly) { params.breached = "true"; }

        return Promise.all([
            api.get("/api/incidents", params),
            api.get("/api/incidents/stats", { days: 30 })
        ])
            .then(function (results) {
                render(results[0], Array.isArray(results[0]) ? null : results[0].pagination);
                renderStats(results[1]);
            })
            .catch(function (error) {
                CP.toastError(error, "Could not load incidents.");
            })
            .finally(function () {
                ui.setLoading(wrap, false);
            });
    }

    /* ----------------------------------------------------------- forms */

    function populateApplications(selected) {
        const select = $("#incidentApplication");
        if (!select) {
            return;
        }
        clear(select);

        if (!CP.store.applications.length) {
            select.appendChild(el("option", { value: "", text: "No applications available" }));
            return;
        }

        CP.store.applications.forEach(function (application) {
            const option = el("option", {
                value: application.id,
                text: application.name + " · " + application.environment
            });
            if (String(application.id) === String(selected)) {
                option.selected = true;
            }
            select.appendChild(option);
        });
    }

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

    function openForm(incident) {
        if (!CP.store.canWrite) {
            CP.toast({ type: "warning", title: "Read only", message: "Your role cannot modify incidents." });
            return;
        }

        state.editing = incident || null;

        $("#incidentFormTitle").textContent = incident ? "Edit incident" : "New incident";
        $("#incidentSubmitBtn").textContent = incident ? "Save changes" : "Create incident";
        $("#incidentIdField").value = incident ? incident.id : "";

        populateApplications(incident ? incident.application_id : null);
        $("#incidentTitle").value = incident ? incident.title : "";
        $("#incidentDescription").value = incident && incident.description ? incident.description : "";
        $("#incidentRootCause").value = incident && incident.root_cause ? incident.root_cause : "";
        $("#incidentAssignee").value = incident && incident.assignee ? incident.assignee : "";

        fillSelect($("#incidentSeverity"), CP.store.metadata.incidents.severities || [],
            incident ? incident.severity : "Medium");
        fillSelect($("#incidentStatus"), CP.store.metadata.incidents.statuses || [],
            incident ? incident.status : "Open");

        CP.modal.open("incidentFormModal");
    }

    function submitForm(event) {
        event.preventDefault();

        const payload = {
            application_id: Number($("#incidentApplication").value),
            title: $("#incidentTitle").value.trim(),
            description: $("#incidentDescription").value.trim(),
            root_cause: $("#incidentRootCause").value.trim(),
            assignee: $("#incidentAssignee").value.trim(),
            severity: $("#incidentSeverity").value,
            status: $("#incidentStatus").value
        };

        if (!payload.application_id || !payload.title) {
            CP.toast({
                type: "warning",
                title: "Missing fields",
                message: "An application and a title are required."
            });
            return;
        }

        const button = $("#incidentSubmitBtn");
        ui.setLoading(button, true);
        button.textContent = "Saving…";

        const editing = state.editing;
        const request = editing
            ? api.put("/api/incidents/" + editing.id, payload)
            : api.post("/api/incidents", payload);

        request
            .then(function (result) {
                CP.modal.close("incidentFormModal");
                CP.toast({
                    type: "success",
                    title: editing ? "Incident updated" : "Incident created",
                    message: result.message
                });
                return load();
            })
            .catch(function (error) {
                CP.toastError(error, "Could not save the incident.");
            })
            .finally(function () {
                ui.setLoading(button, false);
                button.textContent = editing ? "Save changes" : "Create incident";
            });
    }

    /* --------------------------------------------------------- actions */

    function resolve(incident) {
        api.put("/api/incidents/" + incident.id + "/resolve", {})
            .then(function (result) {
                CP.toast({ type: "success", title: result.message, message: incident.title });
                return load();
            })
            .catch(function (error) {
                CP.toastError(error, "Could not resolve the incident.");
            });
    }

    function acknowledge(incident) {
        api.post("/api/incidents/" + incident.id + "/acknowledge")
            .then(function (result) {
                CP.toast({ type: "info", title: result.message, message: incident.title });
                return load();
            })
            .catch(function (error) {
                CP.toastError(error);
            });
    }

    function escalate(incident) {
        api.post("/api/incidents/" + incident.id + "/escalate")
            .then(function (result) {
                CP.toast({
                    type: "warning",
                    title: "Escalated",
                    message: incident.title + " is now " + result.incident.severity
                });
                return load();
            })
            .catch(function (error) {
                CP.toastError(error);
            });
    }

    function remove(incident) {
        CP.modal
            .confirm({
                title: "Delete this incident?",
                message: "'" + incident.title + "' will be permanently removed.",
                confirmLabel: "Delete incident"
            })
            .then(function (confirmed) {
                if (!confirmed) {
                    return null;
                }
                return api.del("/api/incidents/" + incident.id).then(function (result) {
                    state.selected.delete(incident.id);
                    updateSelectionUi();
                    CP.toast({ type: "success", title: result.message });
                    return load();
                });
            })
            .catch(function (error) {
                CP.toastError(error, "Could not delete the incident.");
            });
    }

    function bulkResolve() {
        api.post("/api/incidents/bulk", { action: "resolve", ids: Array.from(state.selected) })
            .then(function (result) {
                CP.toast({ type: "success", title: result.message });
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
        CP.modal
            .confirm({
                title: "Delete " + ids.length + " incidents?",
                message: "This cannot be undone.",
                confirmLabel: "Delete all"
            })
            .then(function (confirmed) {
                if (!confirmed) {
                    return null;
                }
                return api.post("/api/incidents/bulk", { action: "delete", ids: ids })
                    .then(function (result) {
                        CP.toast({ type: "success", title: result.message });
                        state.selected.clear();
                        updateSelectionUi();
                        return load();
                    });
            })
            .catch(function (error) {
                CP.toastError(error);
            });
    }

    /* ---------------------------------------------------------- detail */

    function openDetail(incidentId) {
        api.get("/api/incidents/" + incidentId)
            .then(function (incident) {
                $("#incidentDetailTitle").textContent = incident.title;
                $("#incidentDetailSubtitle").textContent =
                    (incident.application_name || "Unknown application") + " · " +
                    fmt.dateTime(incident.detected_at);

                const body = clear($("#incidentDetailBody"));

                body.appendChild(
                    el("div", { class: "row mb-16" }, [
                        severityBadge(incident.severity),
                        ui.badge(incident.status, incident.status),
                        ui.environmentBadge(incident.application ? incident.application.environment : ""),
                        incident.source === "monitoring" ? ui.badge("Detected automatically", "low") : null
                    ])
                );

                const facts = [
                    ["Assignee", incident.assignee || "Unassigned"],
                    ["Duration", fmt.duration(incident.duration_minutes)],
                    ["Acknowledged", incident.acknowledged_at ? fmt.dateTime(incident.acknowledged_at) : "Not yet"],
                    ["Resolved", incident.resolved_at ? fmt.dateTime(incident.resolved_at) : "Still open"]
                ];

                const grid = el("div", { class: "form-grid mb-16" });
                facts.forEach(function (fact) {
                    grid.appendChild(
                        el("div", { class: "field" }, [
                            el("label", { text: fact[0] }),
                            el("div", { class: "strong", text: String(fact[1]) })
                        ])
                    );
                });
                body.appendChild(grid);

                if (incident.description) {
                    body.appendChild(
                        el("div", { class: "field mb-16" }, [
                            el("label", { text: "Description" }),
                            el("p", { text: incident.description })
                        ])
                    );
                }

                if (incident.root_cause) {
                    body.appendChild(
                        el("div", { class: "field mb-16" }, [
                            el("label", { text: "Root cause" }),
                            el("p", { text: incident.root_cause })
                        ])
                    );
                }

                if (incident.sla_minutes) {
                    body.appendChild(
                        el("div", { class: "field mb-16" }, [
                            el("label", { text: "SLA · target " + incident.sla_minutes + " minutes" }),
                            ui.renderMeter(
                                Math.min(100, (incident.sla_elapsed_minutes / incident.sla_minutes) * 100),
                                incident.sla_breached ? "danger" : "success"
                            ),
                            el("p", { class: "small subtle", text: slaCell(incident).textContent })
                        ])
                    );
                }

                body.appendChild(el("div", { class: "divider" }));
                body.appendChild(el("h3", { class: "mb-16", text: "Timeline" }));

                const timeline = el("div", { class: "timeline" });
                const entries = (incident.timeline || []).slice().reverse();
                if (!entries.length) {
                    timeline.appendChild(ui.emptyState("◷", "No history", "Actions on this incident will appear here."));
                } else {
                    entries.forEach(function (entry) {
                        timeline.appendChild(
                            el("div", { class: "timeline-item" }, [
                                el("strong", { text: entry.summary || entry.action }),
                                el("p", { text: (entry.actor_name || "system") + " · " + CP.fmt.titleCase((entry.action || "").replace(/[._]/g, " ")) }),
                                el("time", { text: fmt.dateTime(entry.created_at) })
                            ])
                        );
                    });
                }
                body.appendChild(timeline);

                const footer = clear($("#incidentDetailFooter"));
                footer.appendChild(el("button", { type: "button", class: "btn", text: "Close", onClick: function () { CP.modal.close("incidentDetailModal"); } }));

                if (CP.store.canWrite) {
                    footer.appendChild(
                        el("button", {
                            type: "button",
                            class: "btn",
                            text: "Edit",
                            onClick: function () {
                                CP.modal.close("incidentDetailModal");
                                openForm(incident);
                            }
                        })
                    );
                }

                if (CP.store.canWrite && incident.status !== "Resolved") {
                    footer.appendChild(
                        el("button", {
                            type: "button",
                            class: "btn btn-primary",
                            text: "Resolve incident",
                            onClick: function () {
                                CP.modal.close("incidentDetailModal");
                                resolve(incident);
                            }
                        })
                    );
                }

                CP.modal.open("incidentDetailModal");
            })
            .catch(function (error) {
                CP.toastError(error, "Could not load the incident.");
            });
    }

    /* ------------------------------------------------------------ init */

    function primeFilters() {
        const statusFilter = $("#incidentStatusFilter");
        const severityFilter = $("#incidentSeverityFilter");

        (CP.store.metadata.incidents.statuses || []).forEach(function (value) {
            statusFilter.appendChild(el("option", { value: value, text: CP.fmt.titleCase(value) }));
        });
        (CP.store.metadata.incidents.severities || []).forEach(function (value) {
            severityFilter.appendChild(el("option", { value: value, text: value }));
        });

        if (state.openOnly) {
            statusFilter.value = "";
        }
    }

    function init() {
        const form = $("#incidentForm");
        if (form) {
            form.addEventListener("submit", submitForm);
        }

        CP.on("click", "#incidentCreateBtn", function () { openForm(null); });
        CP.on("click", "#incidentBulkResolve", bulkResolve);
        CP.on("click", "#incidentBulkDelete", bulkDelete);
        CP.on("click", "#incidentExportBtn", function () {
            api.download("/api/export/incidents");
        });

        const selectAll = $("#incidentSelectAll");
        if (selectAll) {
            selectAll.addEventListener("change", function () {
                const checked = selectAll.checked;
                $$("#incidentsBody input[type=checkbox]").forEach(function (checkbox) {
                    checkbox.checked = checked;
                    const id = Number(checkbox.closest("tr").dataset.id);
                    if (checked) {
                        state.selected.add(id);
                    } else {
                        state.selected.delete(id);
                    }
                });
                updateSelectionUi();
            });
        }

        const search = $("#incidentSearch");
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

        [["#incidentStatusFilter", "status"], ["#incidentSeverityFilter", "severity"]].forEach(function (pair) {
            const node = $(pair[0]);
            if (node) {
                node.addEventListener("change", function () {
                    state[pair[1]] = node.value;
                    state.page = 1;
                    load();
                });
            }
        });

        const openOnly = $("#incidentOpenOnly");
        if (openOnly) {
            openOnly.checked = state.openOnly;
            openOnly.addEventListener("change", function () {
                state.openOnly = openOnly.checked;
                state.page = 1;
                load();
            });
        }

        const breached = $("#incidentBreachedOnly");
        if (breached) {
            breached.addEventListener("change", function () {
                state.breachedOnly = breached.checked;
                state.page = 1;
                load();
            });
        }

        $$("#view-incidents th.sortable").forEach(function (header) {
            header.addEventListener("click", function () {
                const column = header.dataset.sort;
                if (state.sort === column) {
                    state.order = state.order === "asc" ? "desc" : "asc";
                } else {
                    state.sort = column;
                    state.order = "desc";
                }
                state.page = 1;
                load();
            });
        });

        updateSelectionUi();
    }

    global.CloudPulse.views = global.CloudPulse.views || {};
    global.CloudPulse.views.incidents = {
        init: init,
        load: load,
        prime: primeFilters
    };
})(window);
