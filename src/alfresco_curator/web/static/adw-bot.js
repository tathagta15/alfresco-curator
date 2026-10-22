/*
 * Curator launcher for the Alfresco Digital Workspace.
 *
 * Share hands the panel a ticket from a Surf template. ADW has no server-side
 * template, so the ticket is read from where ADF keeps it after sign-in, and
 * minted from the repository only if that fails.
 *
 * Served by the Curator and injected by nginx sub_filter, so the launcher
 * always matches the panel it opens.
 */
(function () {
  "use strict";

  // Derived from this script's own URL, so nothing has to be templated in.
  var SELF = document.currentScript && document.currentScript.src;
  var ORIGIN = SELF
    ? SELF.replace(/^(https?:\/\/[^\/]+).*$/, "$1")
    : window.location.origin;
  var CURATOR_URL = ORIGIN;

  var dock, frame, launcher, loaded = false;

  function style() {
    var css = document.createElement("style");
    css.textContent = [
      "#curator-launcher{position:fixed;right:22px;bottom:22px;z-index:10000;",
      "width:56px;height:56px;border-radius:50%;border:none;",
      "background:linear-gradient(135deg,#1f7ae0,#1559a8);",
      "color:#fff;cursor:pointer;box-shadow:0 6px 20px rgba(21,89,168,.38);font-size:22px;",
      "line-height:56px;text-align:center;padding:0;",
      "transition:transform .2s cubic-bezier(.2,1.2,.4,1),box-shadow .2s ease}",
      "#curator-launcher:hover{transform:scale(1.08) rotate(8deg);",
      "box-shadow:0 10px 28px rgba(21,89,168,.46)}",
      "#curator-launcher:active{transform:scale(.96)}",
      "#curator-launcher .pip{position:absolute;top:3px;right:3px;width:12px;height:12px;",
      "border-radius:50%;background:#10893e;border:2px solid #fff;display:none}",
      "#curator-launcher.ready .pip{display:block;animation:curatorPip 2.4s ease-in-out infinite}",
      "@keyframes curatorPip{0%,100%{box-shadow:0 0 0 0 rgba(16,137,62,.5)}",
      "50%{box-shadow:0 0 0 6px rgba(16,137,62,0)}}",
      "#curator-dock{position:fixed;right:22px;bottom:90px;z-index:10000;width:420px;",
      "height:620px;max-height:calc(100vh - 130px);background:#fff;border:1px solid #e3e8ee;",
      "border-radius:16px;box-shadow:0 20px 56px rgba(16,24,40,.26);overflow:hidden;display:none;",
      "transform-origin:bottom right}",
      "#curator-dock.open{display:block;animation:curatorDock .24s cubic-bezier(.2,1.1,.4,1)}",
      "@keyframes curatorDock{from{opacity:0;transform:translateY(12px) scale(.97)}",
      "to{opacity:1;transform:none}}",
      "#curator-dock iframe{width:100%;height:100%;border:0;display:block}",
      "@media(max-width:520px){#curator-dock{right:10px;left:10px;width:auto}}"
    ].join("");
    document.head.appendChild(css);
  }

  function build() {
    if (document.getElementById("curator-launcher")) { return; }
    style();

    launcher = document.createElement("button");
    launcher.id = "curator-launcher";
    launcher.title = "Curator";
    launcher.innerHTML = "\u2756<span class='pip'></span>";

    dock = document.createElement("div");
    dock.id = "curator-dock";
    frame = document.createElement("iframe");
    frame.title = "Curator";
    dock.appendChild(frame);

    document.body.appendChild(launcher);
    document.body.appendChild(dock);
    launcher.onclick = toggle;
  }

  function toggle() {
    var opening = dock.className !== "open";
    dock.className = opening ? "open" : "";
    if (opening && !loaded) {
      frame.src = CURATOR_URL + "/bot";
      loaded = true;
    }
    if (opening) { watchContext(); } else { stopWatching(); }
  }

  /* ------------------------------------------------------------- context */
  /* What the page can see, so "tag these" and "search here" resolve without
   * the user retyping what is already in front of them.
   *
   * ADW's rows carry an aria-label and aria-selected but no node id, so only
   * names are available here; the Curator resolves them with its own search.
   * ARIA attributes are used in preference to styling classes because they
   * are part of the component contract rather than the theme. */
  var watcher = null, lastSignature = "", lastFolderName = "";

  function readContext() {
    var selected = [];
    var rows = document.querySelectorAll('adf-datatable-row[aria-selected="true"]');
    for (var i = 0; i < rows.length && i < 200; i++) {
      var name = rows[i].getAttribute("aria-label") || "";
      // ADW appends " selected" to the label of a selected row, so the raw
      // attribute would hand the agent a name that does not exist.
      selected.push({ name: name.replace(/\s+selected$/, "") });
    }

    // #/personal-files/<nodeId>, #/libraries/<id>, and so on. The trailing
    // segment is the folder only when it looks like a node id.
    var folderId = "";
    var segments = (window.location.hash || "").split("?")[0].split("/");
    var tail = segments[segments.length - 1] || "";
    if (/^[0-9a-f-]{20,}$/i.test(tail)) { folderId = tail; }

    // While anything is selected ADW replaces the breadcrumb with a
    // "Selected (n)" summary, which is exactly when the folder matters most,
    // so the last real breadcrumb is remembered instead.
    var crumb = document.querySelector("adf-breadcrumb, .adf-breadcrumb");
    var shown = crumb ? (crumb.innerText || "").split("\n").pop().trim() : "";
    if (shown && !/^Selected \(\d+\)$/.test(shown)) { lastFolderName = shown.slice(0, 200); }

    return { folderId: folderId, folderName: lastFolderName, selected: selected };
  }

  function pushContext() {
    if (!loaded || !frame.contentWindow) { return; }
    var context = readContext();
    var signature = JSON.stringify(context);
    if (signature === lastSignature) { return; }
    lastSignature = signature;
    frame.contentWindow.postMessage({ type: "curator:context", context: context }, ORIGIN);
  }

  function watchContext() {
    pushContext();
    // Selection changes fire no event that reaches here, so the page is
    // sampled instead - but only while the panel is open.
    if (!watcher) { watcher = setInterval(pushContext, 1200); }
  }

  function stopWatching() {
    if (watcher) { clearInterval(watcher); watcher = null; }
  }

  // ADF stores the repository ticket here once the user signs in, so the
  // panel can reuse the session rather than asking for credentials again.
  function storedTicket() {
    try {
      return window.localStorage.getItem("ticket-ECM") || "";
    } catch (e) {
      return "";
    }
  }

  function ecmHost() {
    // app.config.json templates the host, so prefer what ADF resolved.
    try {
      var cfg = window.localStorage.getItem("app.config.json");
      if (cfg) {
        var host = JSON.parse(cfg).ecmHost;
        if (host && host.indexOf("{") === -1) { return host; }
      }
    } catch (e) { /* fall through to the current origin */ }
    return window.location.origin;
  }

  function sendTicket(ticket) {
    // An empty baseUrl means "use the address the Curator is configured with",
    // because a browser-facing host does not resolve inside its container.
    frame.contentWindow.postMessage(
      { type: "curator:auth", ticket: ticket, baseUrl: "" }, ORIGIN);
  }

  function mintTicket(done) {
    var stored = storedTicket();
    if (stored) { return done(stored); }

    // Basic auth is still valid for the ticket endpoint when ADF kept
    // credentials rather than a ticket.
    var request = new XMLHttpRequest();
    request.open(
      "GET",
      ecmHost() + "/alfresco/api/-default-/public/authentication/versions/1/tickets/-me-",
      true
    );
    request.withCredentials = true;
    request.setRequestHeader("Accept", "application/json");
    request.onreadystatechange = function () {
      if (request.readyState !== 4) { return; }
      if (request.status !== 200) { return done(""); }
      try {
        done(JSON.parse(request.responseText).entry.id);
      } catch (e) {
        done("");
      }
    };
    request.send();
  }

  window.addEventListener("message", function (event) {
    if (event.origin !== ORIGIN || !event.data || !frame) { return; }

    if (event.data.type === "curator:expand") {
      // Only reached if a panel still relays the click. The panel opens the
      // full screen itself now, because a window.open from here has no user
      // activation and the popup blocker stops it.
      window.open(CURATOR_URL + "/", "curator-full");
      return;
    }
    if (event.data.type === "curator:ready") {
      launcher.className = "ready";
      // No curator:host reply: ADW has no page of its own, so the panel opens
      // the full screen itself and keeps the click's user activation.
      // The panel has a document now, so the first context push can land.
      lastSignature = "";
      pushContext();
      if (!event.data.needsAuth) { return; }
      mintTicket(function (ticket) {
        if (ticket) { sendTicket(ticket); }
      });
    }
  }, false);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
})();
