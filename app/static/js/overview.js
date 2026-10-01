/* Overview view: fleet KPIs, charts and attention list. */
(function (global) {
    "use strict";

    const CP = global.CloudPulse;
    const { $, el, clear, fmt, ui, api } = CP;

    const state = { hours: 24, loading: false };

    const SEVERITY_COLORS = {
        Critical: "#ff5c67",
        High: "#ff8a5c",
        Medium: "#ffb547",
        Low: "#4cc4e0"
    };

    const HEALTH_COLORS = {
        UP: "#35d07f",
        DEGRADED: "#ffb547",
        DOWN: "#ff5c67",
        UNKNOWN: "#6f7ba6"
    };

    function setStat(name, value) {
        const node = document.querySelector('#overviewStats [data-stat="' + name + '"]');
        if (node) {
            node.textContent = value;
        }
    }

    function setHint(name, value) {
        const node = document.querySelector('#overviewStats [data-hint="' + name + '"]');
        if (node) {
            node.textContent = value;
        }
    }

    function renderBanner(data) {
        const banner = $("#systemBanner");
        const status = $("#systemStatus");
        const message = $("#systemMessage");

        const total = data.applications.total;
        const unhealthy = data.applications.unhealthy;
        const degraded = data.applications.degraded;
        const breaches = (data.reliability && data.reliability.sla_breaches) || 0;

        banner.classList.remove("is-info", "is-danger", "is-warning");

        if (total === 0) {
            banner.classList.add("is-info");
            status.textContent = "No applications registered yet";
            message.textContent =
                "Add your first application to start monitoring availability, latency and incidents.";
        } else if (unhealthy > 0) {
            banner.classList.add("is-danger");
            status.textContent =
                unhealthy + " application" + (unhealthy === 1 ? "" : "s") + " failing health checks";
            message.textContent =
                "CloudPulse opened or updated incidents automatically. Open the Incidents view to triage.";
        } else if (degraded > 0) {
            banner.classList.add("is-warning");
            status.textContent = degraded + " application" + (degraded === 1 ? "" : "s") + " degraded";
            message.textContent = "Responses are slower than the latency target. No incidents were opened.";
        } else {
            banner.classList.add("is-info");
            status.textContent = "All monitored applications are healthy";
            message.textContent =
                data.incidents.open === 0
                    ? "No open incidents. Last refreshed " + fmt.relativeTime(data.generated_at) + "."
                    : data.incidents.open + " incident(s) still open from earlier.";
        }

        if (breaches > 0 && unhealthy === 0) {
            message.textContent += " " + breaches + " incident(s) breached their SLA target.";
        }
    }

    function renderStats(data) {
        const applications = data.applications;
        const incidents = data.incidents;
        const reliability = data.reliability || {};

        setStat("applications", fmt.number(applications.total));
        setHint(
            "applications",
            applications.paused
                ? applications.paused + " paused"
                : applications.active + " actively monitored"
        );

        setStat("healthy", fmt.number(applications.healthy));
        setHint("healthy", "of " + applications.active + " active checks");

        setStat("unhealthy", fmt.number(applications.unhealthy));
        setHint("unhealthy", applications.unknown ? applications.unknown + " never checked" : "failing now");

        setStat("openIncidents", fmt.number(incidents.open));
        setHint("openIncidents", incidents.total + " recorded in total");

        setStat("mttr", reliability.mttr_minutes ? fmt.duration(reliability.mttr_minutes) : "—");
        setHint("mttr", "p90 " + (reliability.p90_resolution_minutes ? fmt.duration(reliability.p90_resolution_minutes) : "—"));

        setStat("sla", fmt.percent(reliability.sla_compliance_percentage, 1));
        setHint("sla", (reliability.sla_breaches || 0) + " breach(es) in 30d");

        CP.store.emit("badges", {
            applications: applications.total,
            openIncidents: incidents.open
        });
    }

    function renderTimeseries(payload) {
        const host = $("#timeseriesChart");
        const labels = payload.labels || [];
        const checks = payload.checks || [];
        const failures = payload.failures || [];
        const latency = payload.avg_response_time_ms || [];

        $("#timeseriesHint").textContent =
            fmt.number(checks.reduce(function (sum, value) { return sum + value; }, 0)) +
            " checks in the selected window";

        CP.charts.line(host, {
            labels: labels,
            height: 250,
            title: "Availability and latency",
            formatLabel: function (label) {
                const moment = new Date(label);
                if (Number.isNaN(moment.getTime())) {
                    return label;
                }
                return payload.hours <= 48
                    ? moment.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
                    : moment.toLocaleDateString(undefined, { month: "short", day: "numeric" });
            },
            series: [
                {
                    name: "Avg response time (ms)",
                    values: latency,
                    color: CP.charts.cssVar("--accent", "#6c8bff"),
                    area: true
                },
                {
                    name: "Failed checks",
                    values: failures,
                    color: CP.charts.cssVar("--danger", "#ff5c67"),
                    area: false,
                    dots: false,
                    strokeWidth: 1.8
                }
            ],
            emptyMessage: "No health checks recorded in this window yet"
        });
    }

    function renderHealthDonut(data) {
        const applications = data.applications;
        const slices = [
            { label: "Healthy", value: applications.healthy, color: HEALTH_COLORS.UP },
            { label: "Degraded", value: applications.degraded, color: HEALTH_COLORS.DEGRADED },
            { label: "Down", value: applications.unhealthy, color: HEALTH_COLORS.DOWN },
            { label: "Not checked", value: applications.unknown + applications.never_checked, color: HEALTH_COLORS.UNKNOWN }
        ];

        const active = applications.healthy + applications.degraded + applications.unhealthy;

        CP.charts.donut($("#healthDonut"), {
            slices: slices,
            centerValue: fmt.percent(active ? (applications.healthy / active) * 100 : 0, 0),
            centerLabel: "healthy ratio",
            size: 200,
            emptyMessage: "No applications have been checked yet"
        });
    }

    function renderSeverityChart(data) {
        const breakdown = (data.incidents.by_severity || []).slice().sort(function (a, b) {
            return b.rank - a.rank;
        });

        CP.charts.bars($("#severityChart"), {
            labels: breakdown.map(function (row) { return row.severity; }),
            values: breakdown.map(function (row) { return row.count; }),
            colors: breakdown.map(function (row) { return SEVERITY_COLORS[row.severity]; }),
            height: 210,
            tooltipFormatter: function (value, index) {
                const row = breakdown[index];
                return row.severity + ": " + value + " total, " + row.open + " open";
            },
            emptyMessage: "No incidents have been recorded yet"
        });
    }

    function renderVolumeChart(payload) {
        const labels = payload.labels || [];
        const checks = payload.checks || [];
        const failures = payload.failures || [];

        CP.charts.bars($("#volumeChart"), {
            labels: labels,
            values: checks.map(function (value, index) {
                return Math.max(0, value - (failures[index] || 0));
            }),
            color: CP.charts.cssVar("--success", "#35d07f"),
            height: 210,
            formatLabel: function (label) {
                const moment = new Date(label);
                if (Number.isNaN(moment.getTime())) {
                    return label;
                }
                return payload.hours <= 48
                    ? moment.toLocaleTimeString(undefined, { hour: "2-digit" })
                    : moment.toLocaleDateString(undefined, { month: "short", day: "numeric" });
            },
            emptyMessage: "No checks recorded in this window"
        });
    }

    function renderAttention(apps) {
        const body = clear($("#attentionBody"));

        if (!apps.length) {
            body.appendChild(
                el("tr", null,
                    el("td", { colspan: "4" }, ui.emptyState("✓", "Everything looks healthy", "No application needs attention right now."))
                )
            );
            return;
        }

        apps.forEach(function (application) {
            body.appendChild(
                el("tr", null, [
                    el("td", null, [
                        el("div", { class: "cell-primary", text: application.name }),
                        el("div", { class: "cell-muted truncate", text: application.url })
                    ]),
                    el("td", null, ui.environmentBadge(application.environment)),
                    el("td", null, ui.statusBadge(application.last_health_status)),
                    el("td", { class: "numeric" }, [
                        el("div", { text: fmt.percent(application.uptime_percentage, 1) }),
                        ui.renderMeter(application.uptime_percentage, ui.uptimeVariant(application.uptime_percentage))
                    ])
                ])
            );
        });
    }

    function renderTimeline(entries) {
        const container = clear($("#overviewTimeline"));

        if (!entries.length) {
            container.appendChild(
                ui.emptyState("◷", "No activity yet", "Actions taken in CloudPulse will appear here.")
            );
            return;
        }

        entries.slice(0, 8).forEach(function (entry) {
            container.appendChild(
                el("div", { class: "timeline-item" }, [
                    el("strong", { text: entry.summary || entry.action }),
                    el("p", {
                        text:
                            (entry.actor_name || "system") +
                            " · " +
                            CP.fmt.titleCase((entry.action || "").replace(/[._]/g, " "))
                    }),
                    el("time", { text: fmt.relativeTime(entry.created_at) })
                ])
            );
        });
    }

    async function load() {
        if (state.loading) {
            return;
        }
        state.loading = true;
        $("#refreshIcon").classList.add("dot-pulse");

        try {
            const [overview, series, attention, activity] = await Promise.all([
                api.get("/api/overview"),
                api.get("/api/overview/timeseries", { hours: state.hours }),
                api.get("/api/overview/leaderboard", { limit: 6 }),
                api.get("/api/activity", { per_page: 8 })
            ]);

            renderBanner(overview);
            renderStats(overview);
            renderTimeseries(series);
            renderHealthDonut(overview);
            renderSeverityChart(overview);
            renderVolumeChart(series);
            renderAttention(attention);
            renderTimeline(activity);
        } catch (error) {
            CP.toastError(error, "Could not load the overview.");
        } finally {
            state.loading = false;
            const icon = $("#refreshIcon");
            if (icon) {
                icon.classList.remove("dot-pulse");
            }
        }
    }

    function init() {
        const range = $("#timeseriesRange");
        if (range) {
            range.addEventListener("change", function () {
                state.hours = Number(range.value) || 24;
                api.get("/api/overview/timeseries", { hours: state.hours })
                    .then(renderTimeseries)
                    .catch(function (error) {
                        CP.toastError(error);
                    });
            });
        }
    }

    global.CloudPulse.views = global.CloudPulse.views || {};
    global.CloudPulse.views.overview = { init: init, load: load, reload: load };
})(window);
