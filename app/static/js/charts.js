/* Dependency free SVG charts: line/area, bars, donut and sparkline. */
(function (global) {
    "use strict";

    const NS = "http://www.w3.org/2000/svg";
    const PALETTE = ["#6c8bff", "#35d07f", "#ffb547", "#ff5c67", "#4cc4e0", "#9d7bff"];

    function create(tag, attrs) {
        const node = document.createElementNS(NS, tag);
        Object.entries(attrs || {}).forEach(function ([key, value]) {
            if (value !== null && value !== undefined) {
                node.setAttribute(key, String(value));
            }
        });
        return node;
    }

    function cssVar(name, fallback) {
        const value = getComputedStyle(document.documentElement)
            .getPropertyValue(name)
            .trim();
        return value || fallback;
    }

    function emptyState(host, message) {
        host.innerHTML = "";
        const wrapper = document.createElement("div");
        wrapper.className = "empty";
        const strong = document.createElement("strong");
        strong.textContent = message || "No data available yet";
        wrapper.appendChild(strong);
        host.appendChild(wrapper);
    }

    function niceMax(value) {
        if (value <= 0) {
            return 10;
        }
        const magnitude = Math.pow(10, Math.floor(Math.log10(value)));
        const normalized = value / magnitude;
        let step;
        if (normalized <= 1) {
            step = 1;
        } else if (normalized <= 2) {
            step = 2;
        } else if (normalized <= 5) {
            step = 5;
        } else {
            step = 10;
        }
        return step * magnitude;
    }

    function formatTick(value) {
        const abs = Math.abs(value);
        if (abs >= 1000) {
            return (value / 1000).toFixed(abs >= 10000 ? 0 : 1) + "k";
        }
        if (abs > 0 && abs < 1) {
            return value.toFixed(2);
        }
        return String(Math.round(value * 100) / 100);
    }

    /* ---------------------------------------------------------- line */

    function line(host, config) {
        const labels = config.labels || [];
        const series = (config.series || []).filter(function (item) {
            return item && item.values && item.values.length;
        });

        host.innerHTML = "";

        if (!labels.length || !series.length) {
            emptyState(host, config.emptyMessage);
            return;
        }

        const width = host.clientWidth || 640;
        const height = config.height || 240;
        const padding = { top: 16, right: 16, bottom: 28, left: 46 };
        const plotWidth = Math.max(40, width - padding.left - padding.right);
        const plotHeight = Math.max(40, height - padding.top - padding.bottom);

        const allValues = series.reduce(function (accumulator, item) {
            return accumulator.concat(
                item.values.filter(function (value) {
                    return value !== null && value !== undefined && !Number.isNaN(value);
                })
            );
        }, []);

        const maximum = niceMax(Math.max.apply(null, allValues.concat([1])));
        const minimum = config.zeroBased === false ? Math.min.apply(null, allValues) : 0;
        const range = Math.max(0.0001, maximum - minimum);

        const svg = create("svg", {
            class: "chart",
            viewBox: "0 0 " + width + " " + height,
            preserveAspectRatio: "none",
            role: "img",
            "aria-label": config.title || "Line chart"
        });

        const gridColor = cssVar("--border", "rgba(148,163,214,0.16)");
        const textColor = cssVar("--text-subtle", "#6f7ba6");

        // Horizontal grid + y axis labels
        const gridSteps = 4;
        for (let step = 0; step <= gridSteps; step += 1) {
            const ratio = step / gridSteps;
            const y = padding.top + plotHeight - ratio * plotHeight;
            svg.appendChild(
                create("line", {
                    x1: padding.left,
                    y1: y,
                    x2: padding.left + plotWidth,
                    y2: y,
                    stroke: gridColor,
                    "stroke-width": 1,
                    "stroke-dasharray": step === 0 ? "0" : "3 5"
                })
            );
            const value = minimum + ratio * range;
            const label = create("text", {
                x: padding.left - 9,
                y: y + 4,
                "text-anchor": "end",
                fill: textColor,
                "font-size": "10.5",
                "font-family": "inherit"
            });
            label.textContent = formatTick(value);
            svg.appendChild(label);
        }

        const stepX = labels.length > 1 ? plotWidth / (labels.length - 1) : 0;

        function pointAt(index, value) {
            return {
                x: padding.left + (labels.length > 1 ? index * stepX : plotWidth / 2),
                y:
                    padding.top +
                    plotHeight -
                    ((value - minimum) / range) * plotHeight
            };
        }

        // X axis labels (max 6 to avoid crowding)
        const labelStride = Math.max(1, Math.ceil(labels.length / 6));
        labels.forEach(function (label, index) {
            if (index % labelStride !== 0 && index !== labels.length - 1) {
                return;
            }
            const point = pointAt(index, minimum);
            const text = create("text", {
                x: point.x,
                y: height - 8,
                "text-anchor": "middle",
                fill: textColor,
                "font-size": "10.5",
                "font-family": "inherit"
            });
            text.textContent = config.formatLabel
                ? config.formatLabel(label, index)
                : label;
            svg.appendChild(text);
        });

        series.forEach(function (item, seriesIndex) {
            const color = item.color || PALETTE[seriesIndex % PALETTE.length];
            const points = [];

            item.values.forEach(function (value, index) {
                if (value === null || value === undefined || Number.isNaN(value)) {
                    return;
                }
                points.push(pointAt(index, value));
            });

            if (!points.length) {
                return;
            }

            if (item.area !== false) {
                const areaPath =
                    points
                        .map(function (point, index) {
                            return (index ? "L" : "M") + point.x + " " + point.y;
                        })
                        .join(" ") +
                    " L" +
                    points[points.length - 1].x +
                    " " +
                    (padding.top + plotHeight) +
                    " L" +
                    points[0].x +
                    " " +
                    (padding.top + plotHeight) +
                    " Z";

                const gradientId = "grad-" + seriesIndex + "-" + Math.random().toString(36).slice(2, 8);
                const defs = create("defs");
                const gradient = create("linearGradient", {
                    id: gradientId,
                    x1: "0",
                    y1: "0",
                    x2: "0",
                    y2: "1"
                });
                gradient.appendChild(
                    create("stop", {
                        offset: "0%",
                        "stop-color": color,
                        "stop-opacity": "0.32"
                    })
                );
                gradient.appendChild(
                    create("stop", {
                        offset: "100%",
                        "stop-color": color,
                        "stop-opacity": "0"
                    })
                );
                defs.appendChild(gradient);
                svg.appendChild(defs);

                svg.appendChild(
                    create("path", {
                        d: areaPath,
                        fill: "url(#" + gradient + ")",
                        stroke: "none"
                    })
                );
            }

            const linePath = points
                .map(function (point, index) {
                    return (index ? "L" : "M") + point.x + " " + point.y;
                })
                .join(" ");

            svg.appendChild(
                create("path", {
                    d: linePath,
                    fill: "none",
                    stroke: color,
                    "stroke-width": item.strokeWidth || 2,
                    "stroke-linecap": "round",
                    "stroke-linejoin": "round"
                })
            );

            if (item.dots !== false && points.length <= 40) {
                points.forEach(function (point) {
                    svg.appendChild(
                        create("circle", {
                            cx: point.x,
                            cy: point.y,
                            r: 2.6,
                            fill: color,
                            stroke: cssVar("--bg-elevated", "#111834"),
                            "stroke-width": 1.4
                        })
                    );
                });
            }

            // Invisible hover targets
            points.forEach(function (point, index) {
                const target = create("circle", {
                    cx: point.x,
                    cy: point.y,
                    r: 9,
                    fill: "transparent"
                });
                target.addEventListener("mouseenter", function () {
                    if (config.onHover) {
                        config.onHover(index, point);
                    }
                });
                svg.appendChild(target);
            });
        });

        host.appendChild(svg);

        if (series.length > 1) {
            appendLegend(host, series, config.legendFormatter);
        }
    }

    function appendLegend(host, series, formatter) {
        const legend = document.createElement("div");
        legend.className = "chart-legend";
        series.forEach(function (item, index) {
            const entry = document.createElement("span");
            const swatch = document.createElement("i");
            swatch.style.background =
                item.color || PALETTE[index % PALETTE.length];
            entry.appendChild(swatch);
            entry.appendChild(
                document.createTextNode(
                    formatter ? formatter(item, index) : item.name || "Series " + (index + 1)
                )
            );
            legend.appendChild(entry);
        });
        host.appendChild(legend);
    }

    /* ---------------------------------------------------------- bars */

    function bars(host, config) {
        const labels = config.labels || [];
        const values = (config.values || []).map(function (value) {
            return Number(value) || 0;
        });

        host.innerHTML = "";

        if (!labels.length) {
            emptyState(host, config.emptyMessage);
            return;
        }

        const width = host.clientWidth || 520;
        const height = config.height || 220;
        const padding = { top: 14, right: 12, bottom: 30, left: 42 };
        const plotWidth = Math.max(40, width - padding.left - padding.right);
        const plotHeight = Math.max(30, height - padding.top - padding.bottom);
        const maximum = niceMax(Math.max.apply(null, values.concat([1])));

        const gridColor = cssVar("--border", "rgba(148,163,214,0.16)");
        const textColor = cssVar("--text-subtle", "#6f7ba6");
        const defaultColor = config.color || PALETTE[0];

        const svg = create("svg", {
            class: "chart",
            viewBox: "0 0 " + width + " " + height,
            preserveAspectRatio: "none",
            role: "img",
            "aria-label": config.title || "Bar chart"
        });

        for (let step = 0; step <= 4; step += 1) {
            const ratio = step / 4;
            const y = padding.top + plotHeight - ratio * plotHeight;
            svg.appendChild(
                create("line", {
                    x1: padding.left,
                    y1: y,
                    x2: padding.left + plotWidth,
                    y2: y,
                    stroke: gridColor,
                    "stroke-width": 1,
                    "stroke-dasharray": step === 0 ? "0" : "3 5"
                })
            );
            const label = create("text", {
                x: padding.left - 8,
                y: y + 4,
                "text-anchor": "end",
                fill: textColor,
                "font-size": "10.5",
                "font-family": "inherit"
            });
            label.textContent = formatTick(ratio * maximum);
            svg.appendChild(label);
        }

        const slot = plotWidth / values.length;
        const barWidth = Math.max(4, Math.min(38, slot * 0.62));

        values.forEach(function (value, index) {
            const barHeight = (value / maximum) * plotHeight;
            const x = padding.left + slot * index + (slot - barWidth) / 2;
            const y = padding.top + plotHeight - barHeight;

            const bar = create("rect", {
                x: x,
                y: Math.max(padding.top, y),
                width: barWidth,
                height: Math.max(1, barHeight),
                rx: Math.min(4, barWidth / 3),
                fill: Array.isArray(config.colors)
                    ? config.colors[index % config.colors.length]
                    : defaultColor,
                opacity: 0.9
            });

            const title = create("title");
            title.textContent =
                (config.tooltipFormatter
                    ? config.tooltipFormatter(value, index)
                    : labels[index] + ": " + value) || "";
            bar.appendChild(title);
            svg.appendChild(bar);

            const labelStride = Math.max(1, Math.ceil(labels.length / 8));
            if (index % labelStride === 0) {
                const text = create("text", {
                    x: x + barWidth / 2,
                    y: height - 10,
                    "text-anchor": "middle",
                    fill: textColor,
                    "font-size": "10.5",
                    "font-family": "inherit"
                });
                text.textContent = config.formatLabel
                    ? config.formatLabel(labels[index], index)
                    : labels[index];
                svg.appendChild(text);
            }
        });

        host.appendChild(svg);
    }

    /* --------------------------------------------------------- donut */

    function donut(host, config) {
        const slices = (config.slices || []).filter(function (slice) {
            return Number(slice.value) > 0;
        });

        host.innerHTML = "";

        if (!slices.length) {
            emptyState(host, config.emptyMessage);
            return;
        }

        const total = slices.reduce(function (sum, slice) {
            return sum + Number(slice.value);
        }, 0);

        const size = config.size || 190;
        const radius = size / 2 - 14;
        const strokeWidth = config.thickness || 20;
        const center = size / 2;
        const circumference = 2 * Math.PI * radius;

        const svg = create("svg", {
            class: "chart",
            viewBox: "0 0 " + size + " " + size,
            style: "height:" + size + "px;max-width:" + size + "px;margin:0 auto",
            role: "img",
            "aria-label": config.title || "Donut chart"
        });

        const group = create("g", {
            transform: "rotate(-90 " + center + " " + center + ")"
        });

        group.appendChild(
            create("circle", {
                cx: center,
                cy: center,
                r: radius,
                fill: "none",
                stroke: cssVar("--surface-hover", "rgba(255,255,255,0.06)"),
                "stroke-width": strokeWidth
            })
        );

        let offset = 0;
        slices.forEach(function (slice, index) {
            const fraction = Number(slice.value) / total;
            const dash = fraction * circumference;

            const segment = create("circle", {
                cx: center,
                cy: center,
                r: radius,
                fill: "none",
                stroke: slice.color || PALETTE[index % PALETTE.length],
                "stroke-width": strokeWidth,
                "stroke-dasharray": dash + " " + (circumference - dash),
                "stroke-dashoffset": -offset,
                "stroke-linecap": "butt"
            });

            segment.appendChild(create("title")).textContent =
                slice.label + ": " + slice.value;

            group.appendChild(segment);
            offset += dash;
        });

        svg.appendChild(group);

        if (config.centerValue !== undefined) {
            const value = create("text", {
                x: center,
                y: center - 2,
                "text-anchor": "middle",
                fill: cssVar("--text", "#eef2ff"),
                "font-size": "26",
                "font-weight": "700",
                "font-family": "inherit"
            });
            value.textContent = config.centerValue;
            svg.appendChild(value);

            if (config.centerLabel) {
                const caption = create("text", {
                    x: center,
                    y: center + 18,
                    "text-anchor": "middle",
                    fill: cssVar("--text-subtle", "#6f7ba6"),
                    "font-size": "11",
                    "font-family": "inherit"
                });
                caption.textContent = config.centerLabel;
                svg.appendChild(caption);
            }
        }

        host.appendChild(svg);

        const legend = document.createElement("div");
        legend.className = "chart-legend";
        legend.style.justifyContent = "center";
        slices.forEach(function (slice, index) {
            const entry = document.createElement("span");
            const swatch = document.createElement("i");
            swatch.style.background = slice.color || PALETTE[index % PALETTE.length];
            entry.appendChild(swatch);
            entry.appendChild(
                document.createTextNode(
                    " " + slice.label + " · " + slice.value + " (" + Math.round((Number(slice.value) / total) * 100) + "%)"
                )
            );
            legend.appendChild(entry);
        });
        host.appendChild(legend);
    }

    /* ----------------------------------------------------- sparkline */

    function sparkline(host, values, color) {
        const data = (values || []).filter(function (value) {
            return value !== null && value !== undefined && !Number.isNaN(value);
        });

        host.innerHTML = "";

        if (data.length < 2) {
            host.innerHTML =
                '<span class="subtle small">Not enough data</span>';
            return;
        }

        const width = 110;
        const height = 30;
        const maximum = Math.max.apply(null, data);
        const minimum = Math.min.apply(null, data);
        const range = Math.max(0.0001, maximum - minimum);
        const stroke = color || cssVar("--accent", "#6c8bff");

        const points = data.map(function (value, index) {
            return {
                x: (index / (data.length - 1)) * width,
                y: height - 2 - ((value - minimum) / range) * (height - 4)
            };
        });

        const svg = create("svg", {
            class: "sparkline",
            viewBox: "0 0 " + width + " " + height,
            preserveAspectRatio: "none"
        });

        svg.appendChild(
            create("path", {
                d:
                    points
                        .map(function (point, index) {
                            return (index ? "L" : "M") + point.x + " " + point.y;
                        })
                        .join(" "),
                fill: "none",
                stroke: stroke,
                "stroke-width": 1.6,
                "stroke-linecap": "round",
                "stroke-linejoin": "round"
            })
        );

        const last = points[points.length - 1];
        svg.appendChild(
            create("circle", { cx: last.x, cy: last.y, r: 2.2, fill: stroke })
        );

        host.appendChild(svg);
    }

    global.CloudPulse = Object.assign(global.CloudPulse || {}, {
        charts: {
            line: line,
            bars: bars,
            donut: donut,
            sparkline: sparkline,
            palette: PALETTE,
            cssVar: cssVar
        }
    });
})(window);
