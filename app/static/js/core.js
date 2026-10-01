/* CloudPulse core: DOM helpers, API client, toasts, modals, formatters. */
(function (global) {
    "use strict";

    const API = {};

    /* ------------------------------------------------------------- DOM */

    function $(selector, scope) {
        return (scope || document).querySelector(selector);
    }

    function $$(selector, scope) {
        return Array.from((scope || document).querySelectorAll(selector));
    }

    function el(tag, attrs, children) {
        const node = document.createElement(tag);

        Object.entries(attrs || {}).forEach(([key, value]) => {
            if (value === null || value === undefined || value === false) {
                return;
            }
            if (key === "class") {
                node.className = value;
            } else if (key === "text") {
                node.textContent = value;
            } else if (key === "html") {
                node.innerHTML = value;
            } else if (key === "dataset") {
                Object.entries(value).forEach(([dataKey, dataValue]) => {
                    node.dataset[dataKey] = dataValue;
                });
            } else if (key.startsWith("on") && typeof value === "function") {
                node.addEventListener(key.slice(2).toLowerCase(), value);
            } else {
                node.setAttribute(key, value === true ? "" : value);
            }
        });

        (Array.isArray(children) ? children : children ? [children] : []).forEach(
            (child) => {
                if (child === null || child === undefined || child === false) {
                    return;
                }
                node.appendChild(
                    typeof child === "string" || typeof child === "number"
                        ? document.createTextNode(String(child))
                        : child
                );
            }
        );

        return node;
    }

    function clear(node) {
        while (node && node.firstChild) {
            node.removeChild(node.firstChild);
        }
        return node;
    }

    function escapeHtml(value) {
        if (value === null || value === undefined) {
            return "";
        }
        return String(value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function on(eventName, selector, handler) {
        document.addEventListener(eventName, function (event) {
            const target = event.target.closest(selector);
            if (target) {
                handler(event, target);
            }
        });
    }

    /* ------------------------------------------------------ formatting */

    function number(value) {
        if (value === null || value === undefined || Number.isNaN(value)) {
            return "—";
        }
        return Number(value).toLocaleString();
    }

    function percent(value, digits) {
        if (value === null || value === undefined) {
            return "—";
        }
        return Number(value).toFixed(digits === undefined ? 1 : digits) + "%";
    }

    function duration(minutes) {
        if (minutes === null || minutes === undefined) {
            return "—";
        }
        const total = Math.max(0, Math.round(minutes));
        if (total < 60) {
            return total + "m";
        }
        if (total < 1440) {
            const hours = Math.floor(total / 60);
            const rest = total % 60;
            return hours + "h " + (rest ? rest + "m" : "");
        }
        const days = Math.floor(total / 1440);
        const hours = Math.floor((total % 1440) / 60);
        return days + "d " + (hours ? hours + "h" : "");
    }

    function relativeTime(value) {
        if (!value) {
            return "never";
        }
        const moment = new Date(value);
        if (Number.isNaN(moment.getTime())) {
            return "—";
        }

        const seconds = Math.round((Date.now() - moment.getTime()) / 1000);
        const future = seconds < 0;
        const abs = Math.abs(seconds);

        let text;
        if (abs < 45) {
            return future ? "in a moment" : "just now";
        } else if (abs < 3600) {
            text = Math.round(abs / 60) + "m";
        } else if (abs < 86400) {
            text = Math.round(abs / 3600) + "h";
        } else if (abs < 2592000) {
            text = Math.round(abs / 86400) + "d";
        } else {
            return moment.toLocaleDateString();
        }

        return future ? "in " + text : text + " ago";
    }

    function dateTime(value) {
        if (!value) {
            return "—";
        }
        const moment = new Date(value);
        return Number.isNaN(moment.getTime())
            ? "—"
            : moment.toLocaleString(undefined, {
                  year: "numeric",
                  month: "short",
                  day: "2-digit",
                  hour: "2-digit",
                  minute: "2-digit"
              });
    }

    function shortTime(value) {
        if (!value) {
            return "—";
        }
        const moment = new Date(value);
        return Number.isNaN(moment.getTime())
            ? "—"
            : moment.toLocaleTimeString(undefined, {
                  hour: "2-digit",
                  minute: "2-digit"
              });
    }

    function ms(value) {
        if (value === null || value === undefined) {
            return "—";
        }
        return Number(value) < 1000
            ? Math.round(value) + " ms"
            : (Number(value) / 1000).toFixed(2) + " s";
    }

    function titleCase(value) {
        if (!value) {
            return "—";
        }
        return String(value)
            .replace(/[-_]/g, " ")
            .replace(/\b\w/g, (character) => character.toUpperCase());
    }

    function initials(name) {
        if (!name) {
            return "?";
        }
        return String(name)
            .trim()
            .split(/\s+/)
            .slice(0, 2)
            .map((part) => part.charAt(0).toUpperCase())
            .join("");
    }

    function slug(value) {
        return String(value || "")
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, "-")
            .replace(/^-|-$/g, "");
    }

    /* --------------------------------------------------------- HTTP */

    class ApiError extends Error {
        constructor(message, status, payload) {
            super(message);
            this.name = "ApiError";
            this.status = status;
            this.payload = payload;
            this.fields = (payload && payload.fields) || null;
        }
    }

    async function request(path, options) {
        const settings = Object.assign({ method: "GET" }, options || {});
        const headers = Object.assign(
            {
                Accept: "application/json",
                "X-Requested-With": "XMLHttpRequest"
            },
            settings.headers || {}
        );

        let body = settings.body;
        if (body !== undefined && body !== null && typeof body !== "string") {
            body = JSON.stringify(body);
            headers["Content-Type"] = "application/json";
        }

        let response;
        try {
            response = await fetch(path, {
                method: settings.method,
                headers: headers,
                body: body,
                credentials: "same-origin"
            });
        } catch (networkError) {
            throw new ApiError(
                "Could not reach the CloudPulse API. Check your connection.",
                0,
                null
            );
        }

        if (response.status === 204) {
            return null;
        }

        const contentType = response.headers.get("Content-Type") || "";
        const payload = contentType.includes("application/json")
            ? await response.json().catch(function () {
                  return null;
              })
            : await response.text();

        if (!response.ok) {
            const message =
                (payload && payload.error) ||
                (typeof payload === "string" && payload) ||
                "Request failed with status " + response.status;
            throw new ApiError(message, response.status, payload);
        }

        return payload;
    }

    function get(path, params) {
        const query = buildQuery(params);
        return request(path + query);
    }

    function post(path, body, params) {
        return request(path + buildQuery(params), { method: "POST", body: body });
    }

    function put(path, body, params) {
        return request(path + buildQuery(params), { method: "PUT", body: body });
    }

    function del(path, params) {
        return request(path + buildQuery(params), { method: "DELETE" });
    }

    function buildQuery(params) {
        if (!params) {
            return "";
        }
        const search = new URLSearchParams();
        Object.entries(params).forEach(function ([key, value]) {
            if (value === null || value === undefined || value === "") {
                return;
            }
            search.append(key, value);
        });
        const query = search.toString();
        return query ? "?" + query : "";
    }

    function download(path) {
        const link = el("a", { href: path, download: "" });
        document.body.appendChild(link);
        link.click();
        link.remove();
        toast({
            type: "info",
            title: "Export started",
            message: "Your CSV download should begin shortly."
        });
    }

    /* ------------------------------------------------------- toasts */

    function toast(options) {
        let stack = $(".toast-stack");
        if (!stack) {
            stack = el("div", { class: "toast-stack", role: "status", "aria-live": "polite" });
            document.body.appendChild(stack);
        }

        const settings = Object.assign(
            { type: "info", title: "", message: "", timeout: 4200 },
            options || {}
        );

        const icons = {
            success: "✓",
            error: "✕",
            warning: "!",
            info: "i"
        };

        const node = el("div", { class: "toast toast-" + settings.type }, [
            el("span", { class: "toast-icon", text: icons[settings.type] || "i" }),
            el("div", { class: "toast-body" }, [
                settings.title ? el("strong", { text: settings.title }) : null,
                settings.message ? el("span", { text: settings.message }) : null
            ]),
            el("button", {
                class: "btn btn-ghost btn-sm",
                text: "✕",
                "aria-label": "Dismiss notification",
                onClick: function () {
                    dismiss();
                }
            })
        ]);

        function dismiss() {
            if (!node.isConnected) {
                return;
            }
            node.classList.add("is-leaving");
            setTimeout(function () {
                node.remove();
            }, 180);
        }

        stack.appendChild(node);
        if (settings.timeout > 0) {
            setTimeout(dismiss, settings.timeout);
        }

        return dismiss;
    }

    function toastError(error, fallback) {
        let message = fallback || "Something went wrong.";
        if (error instanceof ApiError) {
            message = error.message;
            if (error.fields) {
                const parts = Object.keys(error.fields).map(function (key) {
                    return key + ": " + error.fields[key];
                });
                message += " (" + parts.join("; ") + ")";
            }
        } else if (error && error.message) {
            message = error.message;
        }

        toast({ type: "error", title: "Request failed", message: message });
    }

    /* ------------------------------------------------------- modals */

    const openModals = [];

    function openModal(id) {
        const backdrop = document.getElementById(id);
        if (!backdrop) {
            return null;
        }
        backdrop.classList.add("is-open");
        document.body.style.overflow = "hidden";

        if (openModals.indexOf(backdrop) === -1) {
            openModals.push(backdrop);
        }

        const focusable = backdrop.querySelector(
            "input, select, textarea, button:not(.modal-close)"
        );
        if (focusable) {
            setTimeout(function () {
                focusable.focus();
            }, 60);
        }

        return backdrop;
    }

    function closeModal(id) {
        const backdrop = typeof id === "string" ? document.getElementById(id) : id;
        if (!backdrop) {
            return;
        }
        backdrop.classList.remove("is-open");
        document.body.style.overflow = "";

        const index = openModals.indexOf(backdrop);
        if (index !== -1) {
            openModals.splice(index, 1);
        }
    }

    function closeTopModal() {
        const backdrop = openModals[openModals.length - 1];
        if (backdrop) {
            closeModal(backdrop);
        }
    }

    function confirmDialog(options) {
        const settings = Object.assign(
            {
                title: "Are you sure?",
                message: "This action cannot be undone.",
                confirmLabel: "Confirm",
                cancelLabel: "Cancel",
                danger: true
            },
            options || {}
        );

        return new Promise(function (resolve) {
            const backdrop = el("div", { class: "modal-backdrop is-open" }, [
                el("div", { class: "modal", style: "max-width:440px" }, [
                    el("div", { class: "modal-header" }, [
                        el("div", null, [
                            el("h3", { text: settings.title }),
                            el("p", { text: settings.message })
                        ])
                    ]),
                    el("div", { class: "modal-footer" }, [
                        el("button", {
                            class: "btn",
                            text: settings.cancelLabel,
                            onClick: function () {
                                close();
                                resolve(false);
                            }
                        }),
                        el("button", {
                            class: "btn " + (settings.danger ? "btn-danger" : "btn-primary"),
                            text: settings.confirmLabel,
                            onClick: function () {
                                close();
                                resolve(true);
                            }
                        })
                    ])
                ])
            ]);

            function close() {
                backdrop.classList.remove("is-open");
                backdrop.remove();
            }

            backdrop.addEventListener("click", function (event) {
                if (event.target === backdrop) {
                    close();
                    resolve(false);
                }
            });

            document.body.appendChild(backdrop);
        });
    }

    /* -------------------------------------------------------- state */

    const store = {
        user: null,
        canWrite: false,
        isAdmin: false,
        applications: [],
        incidents: [],
        deployments: [],
        metadata: { applications: {}, incidents: {}, deployments: {} },
        _listeners: {},

        on: function (event, handler) {
            (this._listeners[event] = this._listeners[event] || []).push(handler);
            return this;
        },

        emit: function (event, payload) {
            (this._listeners[event] || []).forEach(function (handler) {
                try {
                    handler(payload);
                } catch (error) {
                    console.error("Listener failed for " + event, error);
                }
            });
        },

        setUser: function (user) {
            this.user = user;
            const role = (user && user.role) || "Viewer";
            this.isAdmin = role === "Admin";
            this.canWrite = role === "Admin" || role === "Editor";
            this.emit("user", user);
        }
    };

    /* ------------------------------------------------------ helpers */

    function debounce(fn, wait) {
        let timer = null;
        return function () {
            const context = this;
            const args = arguments;
            clearTimeout(timer);
            timer = setTimeout(function () {
                fn.apply(context, args);
            }, wait || 260);
        };
    }

    function badge(label, variant) {
        return el("span", { class: "badge badge-" + slug(variant || label) }, [
            el("i", { class: "dot" }),
            label
        ]);
    }

    function statusBadge(status) {
        const label = status || "UNKNOWN";
        return badge(label, label);
    }

    function environmentBadge(environment) {
        return badge(environment || "Development", environment);
    }

    function emptyState(icon, title, message, action) {
        return el("div", { class: "empty" }, [
            el("div", { class: "empty-icon", text: icon }),
            el("strong", { text: title }),
            message ? el("p", { text: message }) : null,
            action || null
        ]);
    }

    function loadingRow(columns) {
        const cells = [];
        for (let index = 0; index < columns; index += 1) {
            cells.push(
                el("td", null, el("div", { class: "skeleton", style: "height:14px" }))
            );
        }
        return el("tr", null, cells);
    }

    function setLoading(node, isLoading) {
        if (!node) {
            return;
        }
        node.classList.toggle("is-loading", !!isLoading);
    }

    function renderPagination(container, pagination, onPage) {
        if (!container) {
            return;
        }
        clear(container);

        if (!pagination || pagination.total_pages <= 1) {
            if (pagination) {
                container.appendChild(
                    el("span", {
                        class: "subtle small",
                        text: pagination.total + " record" + (pagination.total === 1 ? "" : "s")
                    })
                );
            }
            return;
        }

        const current = pagination.page;
        const last = pagination.total_pages;
        const pages = new Set([1, last, current, current - 1, current + 1]);
        const sorted = Array.from(pages)
            .filter(function (page) {
                return page >= 1 && page <= last;
            })
            .sort(function (a, b) {
                return a - b;
            });

        container.appendChild(
            el("button", {
                text: "‹",
                "aria-label": "Previous page",
                disabled: !pagination.has_previous,
                onClick: function () {
                    onPage(current - 1);
                }
            })
        );

        let previous = 0;
        sorted.forEach(function (page) {
            if (previous && page - previous > 1) {
                container.appendChild(el("span", { class: "subtle", text: "…" }));
            }
            container.appendChild(
                el("button", {
                    text: String(page),
                    class: page === current ? "is-active" : "",
                    onClick: function () {
                        if (page !== current) {
                            onPage(page);
                        }
                    }
                })
            );
            previous = page;
        });

        container.appendChild(
            el("button", {
                text: "›",
                "aria-label": "Next page",
                disabled: !pagination.has_next,
                onClick: function () {
                    onPage(current + 1);
                }
            })
        );

        container.appendChild(
            el("span", {
                class: "subtle small",
                style: "margin-left:8px",
                text:
                    "Page " +
                    current +
                    " of " +
                    last +
                    " · " +
                    pagination.total +
                    " total"
            })
        );
    }

    function renderMeter(value, variant) {
        const clamped = Math.max(0, Math.min(100, Number(value) || 0));
        const meter = el("div", { class: "meter" + (variant ? " is-" + variant : "") }, [
            el("i", { style: "width:" + clamped + "%" })
        ]);
        return meter;
    }

    function uptimeVariant(value) {
        if (value === null || value === undefined) {
            return "";
        }
        if (value >= 99) {
            return "success";
        }
        if (value >= 95) {
            return "warning";
        }
        return "danger";
    }

    function severityOrder(severity) {
        return { Critical: 4, High: 3, Medium: 2, Low: 1 }[severity] || 0;
    }

    global.CloudPulse = Object.assign(global.CloudPulse || {}, {
        $: $,
        $$: $$,
        el: el,
        clear: clear,
        escapeHtml: escapeHtml,
        on: on,
        api: {
            get: get,
            post: post,
            put: put,
            patch: put,
            del: del,
            request: request,
            download: download,
            ApiError: ApiError
        },
        fmt: {
            number: number,
            percent: percent,
            duration: duration,
            relativeTime: relativeTime,
            dateTime: dateTime,
            shortTime: shortTime,
            ms: ms,
            titleCase: titleCase,
            initials: initials,
            slug: slug
        },
        toast: toast,
        toastError: toastError,
        modal: {
            open: openModal,
            close: closeModal,
            closeTop: closeTopModal,
            confirm: confirmDialog
        },
        store: store,
        ui: {
            debounce: debounce,
            badge: badge,
            statusBadge: statusBadge,
            environmentBadge: environmentBadge,
            emptyState: emptyState,
            loadingRow: loadingRow,
            setLoading: setLoading,
            renderPagination: renderPagination,
            renderMeter: renderMeter,
            uptimeVariant: uptimeVariant,
            severityOrder: severityOrder
        }
    });
})(window);
