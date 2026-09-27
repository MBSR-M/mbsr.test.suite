/* Small enhancements for server-rendered pages. No credentials enter browser storage. */
(() => {
  "use strict";

  const storage = {
    get(key, fallback = null) { try { return window.localStorage.getItem(key) ?? fallback; } catch { return fallback; } },
    set(key, value) { try { window.localStorage.setItem(key, value); } catch { /* Storage is optional. */ } },
  };
  const themeQuery = window.matchMedia("(prefers-color-scheme: dark)");
  const mobileQuery = window.matchMedia("(max-width: 820px)");
  const themeNames = new Set(["light", "dark", "system"]);
  let themePreference = storage.get("opengrid.theme", "system");
  if (!themeNames.has(themePreference)) themePreference = "system";

  function applyTheme() {
    const resolved = themePreference === "system" ? (themeQuery.matches ? "dark" : "light") : themePreference;
    document.documentElement.dataset.theme = resolved;
    document.querySelectorAll("[data-theme-select]").forEach((select) => { select.value = themePreference; });
  }
  // Run in the document head so a saved dark preference does not flash a light page.
  applyTheme();

  const charts = new Map();
  let toastTimer;
  let requestCount = 0;

  function toast(message) {
    const target = document.getElementById("app-toast");
    if (!target) return;
    target.textContent = message;
    target.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => { target.hidden = true; }, 8000);
  }

  function closePopovers(except = null) {
    document.querySelectorAll("details[data-popover][open], details.column-controls[open]").forEach((element) => {
      if (element !== except) element.open = false;
    });
  }

  function setSearchVisible(visible) {
    const results = document.getElementById("global-search-results");
    const field = document.getElementById("global-search");
    if (results) results.hidden = !visible;
    if (field) field.setAttribute("aria-expanded", String(visible));
  }

  function syncSidebarAccessibility() {
    const expanded = mobileQuery.matches
      ? document.body.classList.contains("sidebar-mobile-open")
      : !document.body.classList.contains("sidebar-collapsed");
    document.querySelectorAll("[data-sidebar-toggle]").forEach((button) => {
      button.setAttribute("aria-expanded", String(expanded));
    });
    const sidebar = document.getElementById("app-sidebar");
    if (sidebar) sidebar.inert = mobileQuery.matches && !expanded;
  }

  function closeMobileSidebar() {
    document.body.classList.remove("sidebar-mobile-open");
    syncSidebarAccessibility();
  }

  function toggleSidebar() {
    if (mobileQuery.matches) {
      document.body.classList.toggle("sidebar-mobile-open");
    } else {
      document.body.classList.toggle("sidebar-collapsed");
      storage.set("opengrid.sidebar", document.body.classList.contains("sidebar-collapsed") ? "collapsed" : "expanded");
    }
    syncSidebarAccessibility();
  }

  function isSameOrigin(target) {
    try { return new URL(target || window.location.href, window.location.href).origin === window.location.origin; } catch { return false; }
  }

  function csrfToken() { return document.querySelector('meta[name="csrf-token"]')?.content || ""; }

  function protectForm(form) {
    const method = (form.getAttribute("method") || "get").toLowerCase();
    const token = csrfToken();
    if (method === "get" || method === "dialog" || !token || !isSameOrigin(form.action)) return;
    if (!form.querySelector('input[name="csrf_token"]')) {
      const field = document.createElement("input");
      field.type = "hidden";
      field.name = "csrf_token";
      field.value = token;
      form.appendChild(field);
    }
  }

  function columnSettings(tableId) {
    try {
      const parsed = JSON.parse(storage.get(`opengrid.columns.${tableId}`, "{}"));
      return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
    } catch { return {}; }
  }

  function updateColumn(tableId, column, visible) {
    document.querySelectorAll("table[data-table-id]").forEach((table) => {
      if (table.dataset.tableId !== tableId) return;
      table.querySelectorAll("[data-column]").forEach((cell) => {
        if (cell.dataset.column === column) cell.hidden = !visible;
      });
    });
  }

  function initializeColumns() {
    document.querySelectorAll("[data-column-toggle][data-table-target]").forEach((control) => {
      const tableId = control.dataset.tableTarget;
      const column = control.dataset.columnToggle;
      const preferences = columnSettings(tableId);
      const visible = preferences[column] !== false;
      control.checked = visible;
      updateColumn(tableId, column, visible);
    });
  }

  const detailLabels = {
    timestamp: "Time", time: "Time", input_kwh: "Input (kWh)", downstream_kwh: "Downstream (kWh)",
    difference_kwh: "Difference (kWh)", unaccounted_kwh: "Unaccounted (kWh)",
    difference_percent: "Difference (%)", imbalance_percent: "Imbalance (%)",
    completeness: "Completeness (%)", completeness_percent: "Completeness (%)", confidence: "Confidence (%)",
    actual_kwh: "Actual (kWh)", baseline_7d_kwh: "7-day baseline (kWh)", baseline_28d_kwh: "28-day baseline (kWh)",
  };

  function formatValue(value) {
    if (value === null || value === undefined) return "Unavailable";
    if (typeof value === "number") return new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 }).format(value);
    return String(value);
  }

  function detailLines(detail) {
    if (!detail || typeof detail !== "object") return [];
    return Object.entries(detail).map(([key, value]) => `${detailLabels[key] || key.replaceAll("_", " ")}: ${formatValue(value)}`);
  }

  function chartColors() {
    const css = getComputedStyle(document.documentElement);
    const get = (name) => css.getPropertyValue(name).trim();
    return {
      text: get("--chart-text"), grid: get("--chart-grid"), background: get("--surface"),
      palette: [get("--chart-input"), get("--chart-downstream"), get("--chart-difference"), get("--chart-baseline"), get("--red")],
    };
  }

  function showChartPoint(canvas, chart, details, index) {
    const target = document.getElementById(canvas.dataset.chartDetail || "");
    if (!target) return;
    const lines = detailLines(details[index]);
    if (!lines.length) {
      lines.push(formatValue(chart.data.labels?.[index]));
      chart.data.datasets.forEach((dataset) => {
        lines.push(`${dataset.label || "Value"}: ${formatValue(dataset.data[index])}`);
      });
    }
    target.textContent = lines.join(" · ");
    target.hidden = false;
    target.setAttribute("aria-live", "polite");
  }

  function createChart(canvas, source) {
    if (!window.Chart) return;
    let config;
    try { config = JSON.parse(source.textContent); } catch {
      canvas.insertAdjacentText("afterend", "Chart configuration could not be loaded.");
      return;
    }
    if (!config || !config.data || !Array.isArray(config.data.datasets)) return;
    const details = Array.isArray(config.details) ? config.details : [];
    delete config.details;
    const colors = chartColors();
    const cartesian = !["doughnut", "pie", "polarArea", "radar"].includes(config.type);
    config.type = config.type || "line";
    config.data.datasets.forEach((dataset, index) => {
      const color = colors.palette[index % colors.palette.length];
      if (!dataset.borderColor) dataset.borderColor = color;
      if (!dataset.backgroundColor) dataset.backgroundColor = ["doughnut", "pie", "polarArea"].includes(config.type) ? colors.palette : (config.type === "line" ? `${color}18` : color);
      if (config.type === "line" || dataset.type === "line") {
        dataset.borderWidth ??= 2;
        dataset.pointRadius ??= 0;
        dataset.pointHoverRadius ??= 4;
        dataset.pointHitRadius ??= 12;
        dataset.tension ??= 0.2;
        dataset.spanGaps ??= false;
      }
    });
    const originalOptions = config.options || {};
    const originalPlugins = originalOptions.plugins || {};
    const tooltip = originalPlugins.tooltip || {};
    const originalScales = originalOptions.scales || {};
    config.options = {
      ...originalOptions,
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      color: colors.text,
      interaction: { mode: "index", intersect: false, ...(originalOptions.interaction || {}) },
      plugins: {
        ...originalPlugins,
        legend: {
          position: "bottom",
          ...(originalPlugins.legend || {}),
          labels: { color: colors.text, usePointStyle: true, pointStyle: "line", boxWidth: 16, padding: 20, font: { size: 10, family: "system-ui, sans-serif" }, ...(originalPlugins.legend?.labels || {}) },
        },
        tooltip: {
          ...tooltip,
          backgroundColor: colors.background,
          titleColor: colors.text,
          bodyColor: colors.text,
          borderColor: colors.grid,
          borderWidth: 1,
          padding: 12,
          titleFont: { size: 11 },
          bodyFont: { size: 11 },
          callbacks: {
            ...(tooltip.callbacks || {}),
            afterBody: (items) => items.length ? detailLines(details[items[0].dataIndex]) : [],
          },
        },
      },
    };
    if (cartesian) {
      config.options.scales = {
        x: {
          grid: { display: false }, border: { display: false },
          ticks: { color: colors.text, maxTicksLimit: 8, maxRotation: 0, font: { size: 9 } },
          ...(originalScales.x || {}),
        },
        y: {
          grid: { color: colors.grid }, border: { display: false },
          ticks: { color: colors.text, padding: 8, font: { size: 9 } },
          ...(originalScales.y || {}),
        },
      };
      Object.entries(originalScales).forEach(([key, value]) => { if (!Object.hasOwn(config.options.scales, key)) config.options.scales[key] = value; });
    }
    config.options.onClick = (_event, points, chart) => {
      if (points.length) showChartPoint(canvas, chart, details, points[0].index);
    };
    try {
      const chart = new window.Chart(canvas, config);
      charts.set(canvas, chart);
      canvas.setAttribute("role", "img");
      if (!canvas.hasAttribute("aria-label")) canvas.setAttribute("aria-label", "Time series chart. Use the left and right arrow keys to inspect data points.");
      canvas.tabIndex = 0;
      canvas.onkeydown = (event) => {
        if (!["ArrowLeft", "ArrowRight"].includes(event.key) || !chart.data.labels?.length) return;
        event.preventDefault();
        const previous = Number(canvas.dataset.activePoint || -1);
        const index = Math.max(0, Math.min(chart.data.labels.length - 1, previous + (event.key === "ArrowRight" ? 1 : -1)));
        canvas.dataset.activePoint = String(index);
        const points = chart.data.datasets.map((_dataset, datasetIndex) => ({ datasetIndex, index }));
        chart.setActiveElements(points);
        const first = chart.getDatasetMeta(0).data[index];
        if (first && chart.tooltip) chart.tooltip.setActiveElements(points, { x: first.x, y: first.y });
        showChartPoint(canvas, chart, details, index);
        chart.update();
      };
    } catch {
      canvas.insertAdjacentText("afterend", "This chart is temporarily unavailable. The recorded values remain available in the table.");
    }
  }

  function initializeCharts() {
    charts.forEach((chart, canvas) => {
      if (!document.contains(canvas)) { chart.destroy(); charts.delete(canvas); }
    });
    document.querySelectorAll("canvas[data-chart]").forEach((canvas) => {
      if (charts.has(canvas)) return;
      const source = document.getElementById(canvas.dataset.chart);
      if (source?.type === "application/json") createChart(canvas, source);
    });
  }

  function refreshCharts() {
    charts.forEach((chart) => chart.destroy());
    charts.clear();
    initializeCharts();
  }

  function enhance() {
    applyTheme();
    initializeColumns();
    initializeCharts();
    document.querySelectorAll("form").forEach(protectForm);
  }

  function initialize() {
    if (storage.get("opengrid.sidebar") === "collapsed") document.body.classList.add("sidebar-collapsed");
    syncSidebarAccessibility();
    enhance();

    document.addEventListener("click", (event) => {
      const element = event.target instanceof Element ? event.target : null;
      if (!element) return;
      if (element.closest("[data-sidebar-toggle]")) toggleSidebar();
      if (element.closest("[data-sidebar-close]")) closeMobileSidebar();
      const passwordToggle = element.closest("[data-password-toggle]");
      if (passwordToggle) {
        const input = document.getElementById(passwordToggle.dataset.passwordToggle);
        if (input) {
          const visible = input.type === "password";
          input.type = visible ? "text" : "password";
          passwordToggle.textContent = visible ? "Hide" : "Show";
          passwordToggle.setAttribute("aria-label", visible ? "Hide password" : "Show password");
          passwordToggle.setAttribute("aria-pressed", String(visible));
        }
      }
      if (!element.closest("[data-search-container]")) setSearchVisible(false);
      closePopovers(element.closest("details[data-popover], details.column-controls"));
    });

    document.addEventListener("change", (event) => {
      const element = event.target;
      if (!(element instanceof Element)) return;
      if (element.matches("[data-theme-select]")) {
        themePreference = themeNames.has(element.value) ? element.value : "system";
        storage.set("opengrid.theme", themePreference);
        applyTheme();
        refreshCharts();
      }
      if (element.matches("[data-column-toggle][data-table-target]")) {
        const tableId = element.dataset.tableTarget;
        const preferences = columnSettings(tableId);
        preferences[element.dataset.columnToggle] = element.checked;
        storage.set(`opengrid.columns.${tableId}`, JSON.stringify(preferences));
        updateColumn(tableId, element.dataset.columnToggle, element.checked);
      }
      const autoSubmit = element.closest("form[data-auto-submit]");
      if (autoSubmit) autoSubmit.requestSubmit();
    });

    document.addEventListener("focusin", (event) => {
      if (event.target?.id === "global-search") setSearchVisible(Boolean(event.target.value.trim()));
    });
    document.addEventListener("input", (event) => {
      if (event.target?.id === "global-search") setSearchVisible(Boolean(event.target.value.trim()));
    });
    document.addEventListener("keydown", (event) => {
      const active = document.activeElement;
      const typing = active?.matches("input, textarea, select, [contenteditable=true]");
      if (event.key === "/" && !typing && !event.ctrlKey && !event.metaKey && !event.altKey) {
        const field = document.getElementById("global-search");
        if (field) { event.preventDefault(); field.focus(); }
      }
      if (event.key === "Escape") {
        closePopovers();
        setSearchVisible(false);
        if (document.body.classList.contains("sidebar-mobile-open")) {
          closeMobileSidebar();
          document.querySelector("[data-sidebar-toggle]")?.focus();
        }
      }
      if (["ArrowDown", "ArrowUp"].includes(event.key) && active?.closest("[data-search-container]")) {
        const links = Array.from(document.querySelectorAll("#global-search-results a"));
        if (!links.length || document.getElementById("global-search-results")?.hidden) return;
        event.preventDefault();
        const current = links.indexOf(active);
        const next = event.key === "ArrowDown" ? Math.min(links.length - 1, current + 1) : Math.max(0, current - 1);
        links[next].focus();
      }
    });

    document.addEventListener("submit", (event) => {
      if (event.target instanceof HTMLFormElement) protectForm(event.target);
    }, true);
    document.addEventListener("htmx:configRequest", (event) => {
      const token = csrfToken();
      if (token && !["get", "head", "options"].includes(String(event.detail.verb).toLowerCase()) && isSameOrigin(event.detail.path)) {
        event.detail.headers["X-CSRF-Token"] = token;
      }
    });
    document.addEventListener("htmx:beforeRequest", () => {
      requestCount += 1;
      document.body.classList.add("requests-active");
    });
    document.addEventListener("htmx:afterRequest", () => {
      requestCount = Math.max(0, requestCount - 1);
      if (!requestCount) document.body.classList.remove("requests-active");
    });
    document.addEventListener("htmx:afterSwap", (event) => {
      enhance();
      if (event.detail.target?.id === "global-search-results") {
        setSearchVisible(Boolean(document.getElementById("global-search")?.value.trim()));
      }
    });
    document.addEventListener("htmx:responseError", (event) => {
      const status = event.detail.xhr?.status;
      toast(status === 401 ? "Your session has expired. Sign in again to continue." : "The latest update could not be loaded. Your current view is preserved; try again shortly.");
    });
    document.addEventListener("htmx:sendError", () => toast("Connection unavailable. Check your connection and try again."));
    themeQuery.addEventListener("change", () => { if (themePreference === "system") { applyTheme(); refreshCharts(); } });
    mobileQuery.addEventListener("change", () => { closeMobileSidebar(); syncSidebarAccessibility(); });
    window.addEventListener("storage", (event) => {
      if (event.key === "opengrid.theme") {
        themePreference = themeNames.has(event.newValue) ? event.newValue : "system";
        applyTheme();
        refreshCharts();
      }
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, { once: true });
  else initialize();
})();
