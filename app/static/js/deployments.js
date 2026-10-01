/* Deployments view: release history, forms and rollback. */
(function (global) {
    "use strict";

    const CP = global.CloudPulse;
    const { $, $$, el, clear, fmt, ui, api } = CP;

    const state = {
        page: 1,
        perPage: 10,
        sort: "deployed_at",
        order: "desc",
        search: "",
        environment: "",
        status: "",
        editing: null
    };

    function renderRow(deployment) {
        const actions = [];

        if (CP.store.canWrite) {
            actions.push(
                el("button", {
                    type: "button",
                    class: "btn btn-sm btn-ghost",
                    text: "Edit",
                    onClick: function () { openForm(deployment); }
                })
            );
        }

        if (
            CP.store.canWrite &&
            deployment.status !== "Rolled Back" &&
            deployment.status !== "In Progress"
        ) {
            actions.push(
                el("button", {
                    type: "button",
                    class: "btn btn-sm btn-danger",
                    text: "Rollback",
                    onClick: function () { rollback(deployment); }
                })
            );
        }

        if (CP.store.canWrite) {
            actions.push(
                el("button", {
                    type: "button",
                    class: "btn btn-sm btn-danger",
                    text: "Delete",
                    onClick: function () { remove(deployment); }
                })
            );
        }

        return el("tr", { dataset: { id: deployment.id } }, [
            el("td", null, [
                el("div", { class: "cell-primary mono", text: deployment.version }),
                deployment.commit_hash
                    ? el("div", { class: "cell-muted mono", text: String(deployment.commit_hash).slice(0, 12) })
                    : null
            ]),
            el("td", { text: deployment.application_name || "—" }),
            el("td", null, ui.environmentBadge(deployment.environment)),
            el("td", null, ui.badge(deployment.status, deployment.status)),
            el("td", { class: "mono cell-muted truncate", text: deployment.commit_hash || "—" }),
            el("td", { text: deployment.deployed_by || el("span", { class: "subtle", text: "unknown" }) }),
            el("td", { class: "numeric", text: deployment.duration_seconds ? deployment.duration_seconds + "s" : "—" }),
            el("td", { class: "nowrap", text: fmt.relativeTime(deployment.deployed_at) }),
            el("td", { class: "text-right" }, el("div", { class: "btn-group", style: "justify-content:flex-end" }, actions))
        ]);
    }

    function render(payload, pagination) {
        const body = clear($("#deploymentsBody"));
        const rows = Array.isArray(payload) ? payload : payload.data || [];

        if (!rows.length) {
            body.appendChild(
                el("tr", null,
                    el("td", { colspan: "9" },
                        ui.emptyState(
                            "◇",
                            "No deployments recorded",
                            "Log a release to build a delivery timeline and correlate it with incidents.",
                            CP.store.canWrite
                                ? el("button", {
                                    type: "button",
                                    class: "btn btn-primary mt-16",
                                    text: "+ Record deployment",
                                    onClick: function () { openForm(null); }
                                })
                                : null
                        )
                    )
                )
            );
        } else {
            rows.forEach(function (deployment) {
                body.appendChild(renderRow(deployment));
            });
        }

        ui.renderPagination($("#deploymentPagination"), pagination, function (page) {
            state.page = page;
            load();
        });
    }

    function renderStats(summary) {
        if (!summary) {
            return;
        }
        const byStatus = summary.by_status || {};
        const set = function (name, value) {
            const node = document.querySelector('#deploymentStats [data-stat="' + name + '"]');
            if (node) {
                node.textContent = value;
            }
        };

        set("total", fmt.number(summary.total));
        set("successful", fmt.number(byStatus.Successful || 0));
        set("failed", fmt.number(byStatus.Failed || 0));
        set("rolledBack", fmt.number(byStatus["Rolled Back"] || 0));
        set("recent", fmt.number(summary.last_30_days || 0));
    }

    function load() {
        const wrap = $("#deploymentsBody").parentElement;
        ui.setLoading(wrap, true);

        const params = {
            page: state.page,
            per_page: state.perPage,
            sort: state.sort,
            order: state.order
        };

        if (state.search) { params.search = state.search; }
        if (state.environment) { params.environment = state.environment; }
        if (state.status) { params.status = state.status; }

        return api.get("/api/deployments", params)
            .then(function (result) {
                const rows = Array.isArray(result) ? result : result.data || [];
                render(result, Array.isArray(result) ? null : result.pagination);
                renderStats(Array.isArray(result) ? null : result.summary);
                if (Array.isArray(result)) {
                    renderStats({ total: result.length, by_status: {}, last_30_days: 0 });
                }
                return rows;
            })
            .catch(function (error) {
                CP.toastError(error, "Could not load deployments.");
            })
            .finally(function () {
                ui.setLoading(wrap, false);
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

    function populateApplications(selected) {
        const select = $("#deploymentApplication");
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

    function openForm(deployment) {
        if (!CP.store.canWrite) {
            CP.toast({ type: "warning", title: "Read only", message: "Your role cannot modify deployments." });
            return;
        }

        state.editing = deployment || null;

        $("#deploymentFormTitle").textContent = deployment ? "Edit deployment" : "Record deployment";
        $("#deploymentSubmitBtn").textContent = deployment ? "Save changes" : "Save deployment";
        $("#deploymentIdField").value = deployment ? deployment.id : "";

        populateApplications(deployment ? deployment.application_id : null);
        $("#deploymentVersion").value = deployment ? deployment.version : "";
        $("#deploymentCommit").value = deployment && deployment.commit_hash ? deployment.commit_hash : "";
        $("#deploymentChangelog").value = deployment && deployment.changelog ? deployment.changelog : "";
        $("#deploymentDuration").value = deployment && deployment.duration_seconds ? deployment.duration_seconds : "";

        fillSelect($("#deploymentEnvironment"), CP.store.metadata.deployments.environments || [],
            deployment ? deployment.environment : "Production");
        fillSelect($("#deploymentStatus"), CP.store.metadata.deployments.statuses || [],
            deployment ? deployment.status : "Successful");

        CP.modal.open("deploymentFormModal");
    }

    function submitForm(event) {
        event.preventDefault();

        const payload = {
            application_id: Number($("#deploymentApplication").value),
            version: $("#deploymentVersion").value.trim(),
            environment: $("#deploymentEnvironment").value,
            status: $("#deploymentStatus").value,
            commit_hash: $("#deploymentCommit").value.trim() || null,
            changelog: $("#deploymentChangelog").value.trim() || null,
            duration_seconds: $("#deploymentDuration").value
                ? Number($("#deploymentDuration").value)
                : null
        };

        if (!payload.application_id || !payload.version) {
            CP.toast({
                type: "warning",
                title: "Missing fields",
                message: "An application and a version are required."
            });
            return;
        }

        const button = $("#deploymentSubmitBtn");
        ui.setLoading(button, true);
        button.textContent = "Saving…";

        const editing = state.editing;
        const request = editing
            ? api.put("/api/deployments/" + editing.id, payload)
            : api.post("/api/deployments", payload);

        request
            .then(function (result) {
                CP.modal.close("deploymentFormModal");
                CP.toast({
                    type: "success",
                    title: editing ? "Deployment updated" : "Deployment recorded",
                    message: result.message
                });
                return load();
            })
            .catch(function (error) {
                CP.toastError(error, "Could not save the deployment.");
            })
            .finally(function () {
                ui.setLoading(button, false);
                button.textContent = editing ? "Save changes" : "Save deployment";
            });
    }

    /* --------------------------------------------------------- actions */

    function rollback(deployment) {
        CP.modal
            .confirm({
                title: "Roll back " + deployment.version + "?",
                message:
                    "The release will be marked as rolled back for " +
                    (deployment.application_name || "this application") +
                    " in " + deployment.environment + ".",
                confirmLabel: "Roll back"
            })
            .then(function (confirmed) {
                if (!confirmed) {
                    return null;
                }
                return api.post("/api/deployments/" + deployment.id + "/rollback").then(function (result) {
                    CP.toast({ type: "success", title: result.message, message: deployment.version });
                    return load();
                });
            })
            .catch(function (error) {
                CP.toastError(error, "Rollback failed.");
            });
    }

    function remove(deployment) {
        CP.modal
            .confirm({
                title: "Delete deployment " + deployment.version + "?",
                message: "This removes the release from the delivery timeline.",
                confirmLabel: "Delete"
            })
            .then(function (confirmed) {
                if (!confirmed) {
                    return null;
                }
                return api.del("/api/deployments/" + deployment.id).then(function (result) {
                    CP.toast({ type: "success", title: result.message, message: deployment.version });
                    return load();
                });
            })
            .catch(function (error) {
                CP.toastError(error, "Could not delete the deployment.");
            });
    }

    /* ------------------------------------------------------------ init */

    function primeFilters() {
        const envFilter = $("#deploymentEnvFilter");
        const statusFilter = $("#deploymentStatusFilter");

        (CP.store.metadata.deployments.environments || []).forEach(function (value) {
            envFilter.appendChild(el("option", { value: value, text: CP.fmt.titleCase(value) }));
        });
        (CP.store.metadata.deployments.statuses || []).forEach(function (value) {
            statusFilter.appendChild(el("option", { value: value, text: CP.fmt.titleCase(value) }));
        });
    }

    function init() {
        const form = $("#deploymentForm");
        if (form) {
            form.addEventListener("submit", submitForm);
        }

        CP.on("click", "#deploymentCreateBtn", function () { openForm(null); });
        CP.on("click", "#deploymentExportBtn", function () {
            api.download("/api/export/deployments");
        });

        const search = $("#deploymentSearch");
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

        [["#deploymentEnvFilter", "environment"], ["#deploymentStatusFilter", "status"]].forEach(function (pair) {
            const node = $(pair[0]);
            if (node) {
                node.addEventListener("change", function () {
                    state[pair[1]] = node.value;
                    state.page = 1;
                    load();
                });
            }
        });

        $$("#view-deployments th.sortable").forEach(function (header) {
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
    }

    global.CloudPulse.views = global.CloudPulse.views || {};
    global.CloudPulse.views.deployments = {
        init: init,
        load: load,
        prime: primeFilters
    };
})(window);
