/**
 * Command palette per req-web-nav-command-palette.
 *
 * Cmd-K / Ctrl-K (or the chrome affordance button) opens a centered modal with one search input over four
 * groups:
 *   - Recent: the pages and objects this browser opened last (the back-stack), shown on an empty query.
 *   - Pages: registered Pages from /__nav-index.json (tree on an empty query, fuzzy-ranked when typed).
 *   - Resources: domain entities from /__entity-search.json matching the name, an ID or an ARN.
 *   - Commands: scope the tables to one dimension value, or clear the scope (req-web-nav-scope-lens).
 * Enter (or a click) runs the highlighted row: a page navigates; a resource opens in the resource drawer when
 * the page has one, otherwise its full page; Cmd/Ctrl opens either in a new tab. Esc / click-outside closes.
 */
(function () {
  "use strict";

  var RECENT_KEY = "tap-recent";
  var RECENT_MAX = 10;
  var ENTITY_DEBOUNCE_MS = 140;

  var navIndex = null;
  var scopeData = null;
  var modal = null;
  var input = null;
  var resultsList = null;
  var selectedIndex = 0;
  var currentResults = [];
  var entityHits = [];
  var entityTerm = "";
  var entityTimer = null;
  var entityToken = 0;

  // -------------------------------------------------------------------------
  // Recent visits (localStorage, this browser only).
  // -------------------------------------------------------------------------

  function readRecent() {
    try {
      var raw = JSON.parse(window.localStorage.getItem(RECENT_KEY) || "[]");
      return Array.isArray(raw) ? raw.filter(function (r) { return r && r.url && r.title; }) : [];
    } catch (e) {
      return [];
    }
  }

  function remember(entry) {
    if (!entry || !entry.url || !entry.title) return;
    var list = readRecent().filter(function (r) { return r.url !== entry.url; });
    list.unshift({ url: entry.url, title: entry.title, kind: entry.kind || "page", type: entry.type || "" });
    try {
      window.localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, RECENT_MAX)));
    } catch (e) { /* storage full or blocked: recents are a convenience */ }
  }

  function rememberThisPage() {
    var path = window.location.pathname;
    if (path === "/" || /\/edit\/?$/.test(path) || path.indexOf("/auth/") === 0 || path.indexOf("/__") === 0) return;
    var current = document.querySelector("nav[aria-label=Breadcrumb] [aria-current=page]");
    var title = current ? current.textContent.trim() : document.title;
    if (!title) return;
    var isObject = path.indexOf("/object/") === 0;
    remember({ url: path + window.location.search, title: title, kind: isObject ? "resource" : "page" });
  }

  // -------------------------------------------------------------------------
  // Ranking.
  // -------------------------------------------------------------------------

  function isSubsequence(needle, haystack) {
    var i = 0;
    for (var k = 0; k < haystack.length; k++) {
      if (haystack[k] === needle[i]) i++;
      if (i === needle.length) return true;
    }
    return false;
  }

  function scorePage(page, q) {
    var name = (page.name || "").toLowerCase();
    var url = (page.url || "").toLowerCase();
    var desc = (page.description || "").toLowerCase();
    if (name === q) return 1000;
    if (name.indexOf(q) === 0) return 800;
    if (name.indexOf(q) !== -1) return 600;
    if (isSubsequence(q, name)) return 400;
    if (url.indexOf(q) !== -1) return 300;
    if (desc.indexOf(q) !== -1) return 100;
    return 0;
  }

  function rankPages(q, limit) {
    if (!navIndex) return [];
    // Fuzzy score dominates; nav_weight is a clamped tiebreaker (req-web-nav-page-weight-3).
    return navIndex.pages
      .map(function (page) {
        var base = scorePage(page, q);
        if (base === 0) return null;
        var boost = Math.max(-100, Math.min(100, (page.nav_weight || 0) * 5));
        return { score: base + boost, page: page };
      })
      .filter(function (e) { return e !== null; })
      .sort(function (a, b) { return b.score - a.score; })
      .slice(0, limit)
      .map(function (e) { return e.page; });
  }

  /** Empty-query mode: every page in document order, nested under its effective parent. */
  function treePages() {
    if (!navIndex) return [];
    var byParent = new Map();
    navIndex.pages.forEach(function (p) {
      if (p.breadcrumb.length < 2) return;
      var parent = p.breadcrumb[p.breadcrumb.length - 2].url;
      if (!byParent.has(parent)) byParent.set(parent, []);
      byParent.get(parent).push(p);
    });
    var out = [];
    var walk = function (parentUrl, depth) {
      (byParent.get(parentUrl) || [])
        .slice()
        .sort(function (a, b) {
          return (b.nav_weight || 0) - (a.nav_weight || 0) || (a.name || a.url).localeCompare(b.name || b.url);
        })
        .forEach(function (c) {
          out.push({ page: c, depth: depth });
          walk(c.url, depth + 1);
        });
    };
    walk("/", 0);
    return out;
  }

  function scopeCommands(q) {
    var out = [];
    if (!scopeData) return out;
    var wantsScope = q.indexOf("scope") === 0;
    var term = wantsScope ? q.replace(/^scope\s*(to\s*)?/, "") : q;
    if (scopeData.active && (wantsScope || "clear scope".indexOf(q) !== -1)) {
      out.push({ kind: "command", label: "Clear scope", hint: scopeData.active.text, action: { clear: true } });
    }
    if (!wantsScope && q.length < 2) return out;
    (scopeData.options || []).forEach(function (o) {
      var hay = (o.label + " " + o.value + " " + o.keyLabel).toLowerCase();
      if (term && hay.indexOf(term) === -1) return;
      if (!term && !wantsScope) return;
      if (scopeData.active && scopeData.active.key === o.key && scopeData.active.value === o.value) return;
      out.push({
        kind: "command",
        label: "Scope to " + (o.label === o.value ? o.value : o.label + " (" + o.value + ")"),
        hint: o.keyLabel + " · " + o.count,
        action: { key: o.key, value: o.value },
      });
    });
    return out.slice(0, wantsScope ? 12 : 4);
  }

  // Platform tools reached from the palette rather than the chrome (req-web-nav-no-hamburger-3).
  var TOOLS = [
    { label: "Build a query", hint: "Find resources by type, relationship and field", url: "/__query/", words: "build a query builder find search graph relationship" },
  ];

  function toolCommands(q) {
    return TOOLS.filter(function (t) { return !q || t.words.indexOf(q) !== -1; }).map(function (t) {
      return { kind: "command", group: "Commands", label: t.label, hint: t.hint, url: t.url };
    });
  }

  function buildResults(query) {
    var q = query.trim().toLowerCase();
    var out = [];
    if (!q) {
      readRecent().slice(0, 5).forEach(function (r) {
        out.push({ kind: "recent", group: "Recent", label: r.title, hint: r.url, url: r.url });
      });
      scopeCommands("").forEach(function (c) { c.group = "Commands"; out.push(c); });
      toolCommands("").forEach(function (c) { out.push(c); });
      treePages().forEach(function (t) {
        out.push({ kind: "page", group: "Pages", label: t.page.name || t.page.url, hint: t.page.url, url: t.page.url, depth: t.depth });
      });
      return out;
    }
    rankPages(q, 6).forEach(function (p) {
      out.push({ kind: "page", group: "Pages", label: p.name || p.url, hint: p.url, url: p.url });
    });
    if (entityTerm === q) {
      entityHits.forEach(function (h) {
        out.push({
          kind: "resource",
          group: "Resources",
          label: h.name,
          hint: h.type_name,
          url: h.page_url,
          entityId: h.entity_id,
          entityType: h.entity_type,
        });
      });
    }
    scopeCommands(q).forEach(function (c) { c.group = "Commands"; out.push(c); });
    if (q.length >= 2) toolCommands(q).forEach(function (c) { out.push(c); });
    return out;
  }

  // -------------------------------------------------------------------------
  // Data.
  // -------------------------------------------------------------------------

  function loadNavIndex() {
    if (navIndex) return Promise.resolve();
    return fetch("/__nav-index.json", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) { navIndex = d; })
      .catch(function (e) { console.error("Failed to load nav index:", e); navIndex = { pages: [] }; });
  }

  function loadScope() {
    return fetch("/__scope.json", { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        (d.options || []).forEach(function (o) {
          o.keyLabel = o.key.replace(/[._]/g, " ").replace(/\b\w/g, function (c) { return c.toUpperCase(); }).replace(/\bAws\b/, "AWS");
        });
        scopeData = d;
      })
      .catch(function () { scopeData = null; });
  }

  function queueEntitySearch(q) {
    if (entityTimer) window.clearTimeout(entityTimer);
    if (q.length < 2) { entityHits = []; entityTerm = q; return; }
    entityTimer = window.setTimeout(function () {
      var mine = ++entityToken;
      fetch("/__entity-search.json?q=" + encodeURIComponent(q), { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : { entities: [] }; })
        .then(function (d) {
          if (mine !== entityToken) return;
          entityHits = d.entities || [];
          entityTerm = q;
          renderResults(true);
        })
        .catch(function () {});
    }, ENTITY_DEBOUNCE_MS);
  }

  // -------------------------------------------------------------------------
  // Modal DOM.
  // -------------------------------------------------------------------------

  function createModal() {
    var overlay = document.createElement("div");
    overlay.className = "tap-palette-overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-label", "Command palette");

    var box = document.createElement("div");
    box.className = "tap-palette-box";

    input = document.createElement("input");
    input.type = "text";
    input.className = "tap-palette-input";
    input.setAttribute("placeholder", "Search pages, resources, IDs or ARNs… (type “scope” to scope tables)");
    input.setAttribute("aria-label", "Search pages and resources");
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "true");
    input.setAttribute("aria-controls", "tap-palette-results");
    input.setAttribute("autocomplete", "off");
    input.setAttribute("autocorrect", "off");
    input.setAttribute("autocapitalize", "off");
    input.setAttribute("spellcheck", "false");

    resultsList = document.createElement("ul");
    resultsList.className = "tap-palette-results";
    resultsList.id = "tap-palette-results";
    resultsList.setAttribute("role", "listbox");
    resultsList.setAttribute("aria-label", "Results");

    var foot = document.createElement("div");
    foot.className = "tap-palette-foot";
    foot.textContent = "Enter opens · ⌘/Ctrl+Enter opens in a new tab · Esc closes";

    box.appendChild(input);
    box.appendChild(resultsList);
    box.appendChild(foot);
    overlay.appendChild(box);
    document.body.appendChild(overlay);

    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) closePalette();
    });
    input.addEventListener("input", function () {
      queueEntitySearch(input.value.trim().toLowerCase());
      renderResults(false);
    });
    input.addEventListener("keydown", handleKeyNav);
    return overlay;
  }

  function renderResults(keepSelection) {
    var keepKey = keepSelection && currentResults[selectedIndex] ? currentResults[selectedIndex].label : null;
    currentResults = buildResults(input.value);
    resultsList.innerHTML = "";
    selectedIndex = 0;

    if (currentResults.length === 0) {
      var li = document.createElement("li");
      li.className = "tap-palette-empty";
      var q = input.value.trim();
      li.textContent = !navIndex ? "Loading…" : q.length >= 2 && entityTerm !== q.toLowerCase() ? "Searching…" : "Nothing matches";
      resultsList.appendChild(li);
      input.removeAttribute("aria-activedescendant");
      return;
    }

    var lastGroup = null;
    currentResults.forEach(function (item, i) {
      if (keepKey && item.label === keepKey) selectedIndex = i;
      if (item.group !== lastGroup) {
        lastGroup = item.group;
        var head = document.createElement("li");
        head.className = "tap-palette-group";
        head.setAttribute("role", "presentation");
        head.textContent = item.group;
        resultsList.appendChild(head);
      }
      var row = document.createElement("li");
      row.className = "tap-palette-result tap-palette-result--" + item.kind;
      row.id = "tap-palette-opt-" + i;
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", "false");
      if (item.url) row.setAttribute("data-url", item.url);
      if (typeof item.depth === "number") row.style.paddingLeft = (0.5 + item.depth * 1.25) + "rem";

      var name = document.createElement("span");
      name.className = "tap-palette-result-name";
      name.textContent = item.label;
      var hint = document.createElement("span");
      hint.className = "tap-palette-result-url";
      hint.textContent = item.hint || "";
      row.appendChild(name);
      row.appendChild(hint);

      row.addEventListener("click", function (e) { run(item, e); });
      row.addEventListener("mouseenter", function () { selectedIndex = i; updateSelection(); });
      resultsList.appendChild(row);
    });
    updateSelection();
  }

  function updateSelection() {
    var items = resultsList.querySelectorAll(".tap-palette-result");
    items.forEach(function (item, i) {
      var on = i === selectedIndex;
      item.classList.toggle("is-selected", on);
      item.setAttribute("aria-selected", on ? "true" : "false");
      if (on) {
        input.setAttribute("aria-activedescendant", item.id);
        item.scrollIntoView({ block: "nearest" });
      }
    });
  }

  function handleKeyNav(e) {
    if (e.key === "Escape") {
      closePalette();
      e.preventDefault();
    } else if (e.key === "ArrowDown") {
      selectedIndex = Math.min(selectedIndex + 1, currentResults.length - 1);
      updateSelection();
      e.preventDefault();
    } else if (e.key === "ArrowUp") {
      selectedIndex = Math.max(selectedIndex - 1, 0);
      updateSelection();
      e.preventDefault();
    } else if (e.key === "Enter") {
      var item = currentResults[selectedIndex];
      if (item) run(item, e);
      e.preventDefault();
    }
  }

  // -------------------------------------------------------------------------
  // Actions.
  // -------------------------------------------------------------------------

  function newTab(event) {
    return event && (event.metaKey || event.ctrlKey);
  }

  function navigate(url, event) {
    if (newTab(event)) {
      window.open(url, "_blank", "noopener");
      return;
    }
    window.location.href = url;
  }

  function setScope(action) {
    var body = new URLSearchParams();
    if (action.clear) body.set("clear", "1");
    else { body.set("key", action.key); body.set("value", action.value); }
    var token = document.querySelector('meta[name="csrf-token"]');
    return fetch("/__scope.json", {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "X-CSRFToken": token ? token.content : "" },
      body: body,
    }).then(function (r) {
      if (r.ok) window.location.reload();
    });
  }

  function run(item, event) {
    if (item.kind === "command" && item.action) {
      closePalette();
      setScope(item.action);
      return;
    }
    if (item.kind === "resource") {
      remember({ url: item.url, title: item.label, kind: "resource" });
      if (!newTab(event) && !(event && event.shiftKey) && window.tapDrawer && document.querySelector("[data-tap-drawer-region]")) {
        closePalette();
        window.tapDrawer.open(item.entityType, item.entityId, input);
        return;
      }
    }
    navigate(item.url, event);
  }

  function openPalette() {
    if (!modal) modal = createModal();
    modal.classList.add("is-open");
    input.value = "";
    entityHits = [];
    entityTerm = "";
    renderResults(false);
    input.focus();
    Promise.all([loadNavIndex(), loadScope()]).then(function () { renderResults(false); });
  }

  function closePalette() {
    if (modal) modal.classList.remove("is-open");
  }

  document.addEventListener("keydown", function (e) {
    if ((e.metaKey || e.ctrlKey) && e.key === "k") {
      openPalette();
      e.preventDefault();
    }
  });

  // A resource opened in the drawer joins the recents with its own name.
  document.addEventListener("tap:drawer-change", function () {
    var head = document.querySelector("[data-tap-drawer-region] [data-tap-drawer-entity]");
    var title = document.getElementById("tap-drawer-title");
    var open = document.querySelector("[data-tap-drawer-region] .tap-drawer-open");
    if (head && title && open) remember({ url: open.getAttribute("href"), title: title.textContent.trim(), kind: "resource" });
  });

  function wireAffordance() {
    rememberThisPage();
    var btn = document.querySelector("[data-tap-palette-affordance]");
    if (btn) {
      btn.addEventListener("click", function (e) {
        e.preventDefault();
        openPalette();
      });
      var isMac = /mac|iphone|ipad|ipod/i.test(
        (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || ""
      );
      if (!isMac) {
        btn.title = "Search pages and resources (Ctrl+K)";
        var kbd = btn.querySelector(".tap-palette-affordance-kbd");
        if (kbd) kbd.textContent = "Ctrl+K";
      }
    }
  }

  window.tapPalette = { open: openPalette };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireAffordance);
  } else {
    wireAffordance();
  }
})();
