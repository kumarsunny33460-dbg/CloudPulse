/* Application shell: identity, navigation, theme, auto refresh. */
(function (global) {
    "use strict";

    const CP = global.CloudPulse;
    const { $, $$, el, fmt, api } = CP;

    const THEME_KEY = "cloudpulse.theme";
    const REFRESH_KEY = "cloudpulse.autoRefresh";

    const VIEW_META = {
        overview: { title: "Overview", subtitle: "Fleet health, reliability and recent activity" },
        applications: { title: "Applications", subtitle: "Every service CloudPulse keeps an eye on" },
        incidents: { title: "Incidents", subtitle: "Triage, escalate and resolve issues" },
        deployments: { title: "Deployments", subtitle: "Release history across environments" },
        activity: { title: "Activity", subtitle: "Full audit trail of platform changes" }
    };

    const state = {
        view: "overview",
        timer: null,
        autoRefresh: 30000
    };

    /* ------------------------------------------------------------ theme */

    function applyTheme(theme) {
        document.documentElement.setAttribute("data-theme", theme);
        try {
            localStorage.setItem(THEME_KEY, theme);
        } catch (error) {
            /* storage unavailable, ignore */
        }
    }

    function initTheme() {
        let stored = null;
        try {
            stored = localStorage.getItem(THEME_KEY);
        } catch (error) {
            stored = null;
        }

        if (stored) {
            applyTheme(stored);
            return;
        }

        const prefersLight =
            global.matchMedia && global.matchMedia("(prefers-color-scheme: light)").matches;
        applyTheme(prefersLight ? "light" : "dark");
    }

    /* ------------------------------------------------------ navigation */

    function showView(name) {
        if (!VIEW_META[name]) {
            return;
        }

        state.view = name;

        $$(".view").forEach(function (view) {
            view.classList.toggle("is-active", view.id === "view-" + name);
        });

        $$(".nav-item").forEach(function (item) {
            item.classList.toggle("is-active", item.dataset.view === name);
        });

        $("#pageTitle").textContent = VIEW_META[name].title;
        $("#pageSubtitle").textContent = VIEW_META[name].subtitle;
        document.title = VIEW_META[name].title + " | CloudPulse";

        const sidebar = $("#sidebar");
        if (sidebar) {
            sidebar.classList.remove("is-open");
        }

        if (location.hash !== "#" + name) {
            history.replaceState(null, "", "#" + name);
        }

        refreshView(name);
    }

    function refreshView(name) {
        const views = CP.views || {};
        const target = views[name];
        if (target && typeof target.load === "function") {
            target.load();
        }
    }

    function refreshAll() {
        loadApplications();
        refreshView(state.view);
    }

    /* --------------------------------------------------------- identity */

    function applyIdentity(user) {
        const name = (user && user.username) || "User";
        const role = (user && user.role) || "Viewer";

        $("#currentUsername").textContent = name;
        $("#currentUserRole").textContent = role;
        $("#userAvatar").textContent = fmt.initials(name);

        const canWrite = CP.store.canWrite;
        const targets = [
            "#appCreateBtn",
            "#incidentCreateBtn",
            "#deploymentCreateBtn"
        ];
        targets.forEach(function (selector) {
            const node = $(selector);
            if (!node) {
                return;
            }
            if (canWrite) {
                node.classList.remove("hidden");
            } else {
                node.classList.add("hidden");
                node.title = "Your role (" + role + ") is read only";
            }
        });
    }

    function updateBadges(counts) {
        const appBadge = $("#navAppCount");
        if (appBadge) {
            appBadge.textContent = counts.applications;
            appBadge.classList.toggle("hidden", !counts.applications);
        }

        const incidentBadge = $("#navOpenIncidents");
        if (incidentBadge) {
            incidentBadge.textContent = counts.openIncidents;
            incidentBadge.classList.toggle("hidden", !counts.openIncidents);
        }
    }

    /* -------------------------------------------------------- data load */

    function loadApplications() {
        return api.get("/api/applications", { per_page: 200, order: "asc" })
            .then(function (result) {
                CP.store.applications = Array.isArray(result) ? result : result.data || [];
            })
            .catch(function (error) {
                console.error("Could not load the application list", error);
            });
    }

    function loadMetadata() {
        return Promise.all([
            api.get("/api/applications/metadata"),
            api.get("/api/incidents/metadata"),
            api.get("/api/deployments/metadata")
        ])
            .then(function (results) {
                CP.store.metadata.applications = results[0];
                CP.store.metadata.incidents = results[1];
                CP.store.metadata.deployments = results[2];

                (CP.views.applications || {}).prime &&
                    CP.views.applications.prime();
                (CP.views.incidents || {}).prime && CP.views.incidents.prime();
                (CP.views.deployments || {}).prime && CP.views.deployments.prime();
            })
            .catch(function (error) {
                console.error("Could not load metadata", error);
            });
    }

    function loadIdentity() {
        return api.get("/api/me")
            .then(function (payload) {
                CP.store.setUser(payload.user);
                applyIdentity(payload.user);
            })
            .catch(function (error) {
                if (error.status === 401) {
                    global.location.href = "/login";
                    return;
                }
                CP.toastError(error, "Could not load your profile.");
            });
    }

    /* ------------------------------------------------------ auto refresh */

    function setAutoRefresh(enabled) {
        if (state.timer) {
            clearInterval(state.timer);
            state.timer = null;
        }

        const badge = $("#liveBadge");
        try {
            localStorage.setItem(REFRESH_KEY, enabled ? "1" : "0");
        } catch (error) {
            /* ignore */
        }

        if (badge) {
            badge.classList.toggle("is-warning", !enabled);
            clear(badge);
            badge.appendChild(el("i", { class: "dot" + (enabled ? " dot-pulse" : "") }));
            badge.appendChild(document.createTextNode(enabled ? " Live" : " Paused"));
        }

        if (!enabled) {
            return;
        }

        state.timer = setInterval(function () {
            if (document.hidden) {
                return;
            }
            refreshAll();
        }, state.autoRefresh);
    }

    /* ------------------------------------------------------------- init */

    function initModals() {
        document.addEventListener("click", function (event) {
            const closer = event.target.closest("[data-close-modal]");
            if (closer) {
                const backdrop = closer.closest(".modal-backdrop");
                if (backdrop) {
                    CP.modal.close(backdrop);
                }
                return;
            }

            if (event.target.classList && event.target.classList.contains("modal-backdrop")) {
                CP.modal.close(event.target);
            }
        });

        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape") {
                CP.modal.closeTop();
            }
        });
    }

    function initShortcuts() {
        document.addEventListener("keydown", function (event) {
            const typing =
                ["INPUT", "TEXTAREA", "SELECT"].indexOf(document.activeElement.tagName) !== -1;
            if (typing || event.metaKey || event.ctrlKey || event.altKey) {
                return;
            }

            if (event.key === "r") {
                event.preventDefault();
                refreshAll();
                CP.toast({ type: "info", title: "Refreshing", message: "Latest data loaded.", timeout: 1600 });
            }

            if (event.key >= "1" && event.key <= "5") {
                const names = Object.keys(VIEW_META);
                const name = names[Number(event.key) - 1];
                if (name) {
                    showView(name);
                }
            }
        });
    }

    function init() {
        initTheme();
        initModals();
        initShortcuts();

        Object.keys(CP.views || {}).forEach(function (name) {
            if (typeof CP.views[name].init === "function") {
                CP.views[name].init();
            }
        });

        $$(".nav-item").forEach(function (item) {
            item.addEventListener("click", function () {
                showView(item.dataset.view);
            });
        });

        CP.on("click", "[data-goto]", function (event, target) {
            showView(target.dataset.goto);
        });

        const toggle = $("#themeToggle");
        if (toggle) {
            toggle.addEventListener("click", function () {
                const current = document.documentElement.getAttribute("data-theme");
                applyTheme(current === "dark" ? "light" : "dark");
                refreshView(state.view);
            });
        }

        const sidebarToggle = $("#sidebarToggle");
        if (sidebarToggle) {
            sidebarToggle.addEventListener("click", function () {
                $("#sidebar").classList.toggle("is-open");
            });
        }

        const refreshBtn = $("#refreshBtn");
        if (refreshBtn) {
            refreshBtn.addEventListener("click", function () {
                const icon = $("#refreshIcon");
                if (icon) {
                    icon.classList.add("dot-pulse");
                }
                refreshAll();
                setTimeout(function () {
                    if (icon) {
                        icon.classList.remove("dot-pulse");
                    }
                }, 700);
            });
        }

        const badge = $("#liveBadge");
        if (badge) {
            badge.style.cursor = "pointer";
            badge.title = "Toggle automatic refresh (every 30 seconds)";
            badge.addEventListener("click", function () {
                setAutoRefresh(!state.timer);
            });
        }

        window.addEventListener("hashchange", function () {
            const name = location.hash.replace("#", "");
            if (name && name !== state.view) {
                showView(name);
            }
        });

        CP.store.on("badges", updateBadges);

        let autoRefreshEnabled = "1";
        try {
            autoRefreshEnabled = localStorage.getItem(REFRESH_KEY) || "1";
        } catch (error) {
            autoRefreshEnabled = "1";
        }

        loadIdentity()
            .then(loadApplications)
            .then(loadMetadata)
            .then(function () {
                const initial = location.hash.replace("#", "");
                showView(VIEW_META[initial] ? initial : "overview");
                setAutoRefresh(autoRefreshEnabled === "1");
                CP.toast({
                    type: "success",
                    title: "Welcome back, " + ((CP.store.user && CP.store.user.username) || "there"),
                    message: "CloudPulse is monitoring " +
                        CP.store.applications.length + " application(s).",
                    timeout: 3200
                });
            });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

    global.CloudPulse.shell = { showView: showView, refreshAll: refreshAll };
})(window);
