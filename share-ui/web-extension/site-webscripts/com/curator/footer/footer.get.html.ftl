<#--
   Floating Curator launcher, appended to the global footer.

   The footer renders on every Share template and its markup is wrapped in
   <@markup> blocks, so this extends the component rather than replacing it.
-->
<@markup id="curatorBot" target="html" action="after">
<#assign curatorBotUrl = (curatorUrl!"http://localhost:8600")?html>

<style type="text/css">
   #curator-launcher {
      position: fixed; right: 22px; bottom: 22px; z-index: 10000;
      width: 56px; height: 56px; border-radius: 50%; border: none;
      background: linear-gradient(135deg, #1f7ae0, #1559a8); color: #fff; cursor: pointer;
      box-shadow: 0 6px 20px rgba(21,89,168,.38);
      font-size: 22px; line-height: 56px; text-align: center; padding: 0;
      transition: transform 200ms cubic-bezier(.2,1.2,.4,1), box-shadow 200ms ease;
   }
   #curator-launcher:hover {
      transform: scale(1.08) rotate(8deg);
      box-shadow: 0 10px 28px rgba(21,89,168,.46);
   }
   #curator-launcher:active { transform: scale(.96); }
   #curator-launcher .pip {
      position: absolute; top: 3px; right: 3px;
      width: 12px; height: 12px; border-radius: 50%;
      background: #10893e; border: 2px solid #fff; display: none;
   }
   #curator-launcher.ready .pip { display: block; animation: curatorPip 2.4s ease-in-out infinite; }

   @keyframes curatorPip {
      0%, 100% { box-shadow: 0 0 0 0 rgba(16,137,62,.5); }
      50%      { box-shadow: 0 0 0 6px rgba(16,137,62,0); }
   }

   #curator-dock {
      position: fixed; right: 22px; bottom: 90px; z-index: 10000;
      width: 420px; height: 620px; min-width: 320px; min-height: 380px;
      max-width: calc(100vw - 32px); max-height: calc(100vh - 130px); resize: both;
      background: #fff; border: 1px solid #e3e8ee; border-radius: 16px;
      box-shadow: 0 20px 56px rgba(16,24,40,.26);
      overflow: auto; display: none; transform-origin: bottom right;
   }
   #curator-dock.open { display: block; animation: curatorIn 240ms cubic-bezier(.2,1.1,.4,1); }
   #curator-dock iframe { width: 100%; height: 100%; border: 0; display: block; }

   @keyframes curatorIn {
      from { opacity: 0; transform: translateY(12px) scale(.97); }
      to   { opacity: 1; transform: none; }
   }

   @media (max-width: 520px) {
      #curator-dock {
         right: 10px; left: 10px; width: auto; height: calc(100vh - 130px);
         min-width: 0; min-height: 0; max-width: none; resize: none;
      }
   }
   @media (min-width: 521px) and (max-height: 520px) {
      #curator-dock {
         bottom: 10px; height: calc(100vh - 20px);
         min-height: 240px; max-height: calc(100vh - 20px);
      }
   }
</style>

<script type="text/javascript">//<![CDATA[
(function () {
   var CURATOR_URL = "${curatorBotUrl}";
   var ORIGIN = CURATOR_URL.replace(/^(https?:\/\/[^\/]+).*$/, "$1");
   // Share proxies the repository under "alfresco-api"; this is the same URL
   // the full Curator page uses for its handshake.
   var TICKET_URL = Alfresco.constants.PROXY_URI_RELATIVE
      ? Alfresco.constants.URL_CONTEXT + "proxy/alfresco-api/-default-/public/authentication/versions/1/tickets/-me-"
      : "/share/proxy/alfresco-api/-default-/public/authentication/versions/1/tickets/-me-";

   var dock, frame, launcher, loaded = false, dockResizeObserver, dockResizeTimer;

   function restoreDockSize() {
      if (!dock) { return; }
      if (window.innerWidth <= 520) {
         dock.style.width = "";
         dock.style.height = "";
         return;
      }
      try {
         var saved = JSON.parse(window.localStorage.getItem("curator.share.dock-size.v1") || "null");
         if (saved && typeof saved.width === "number" && typeof saved.height === "number") {
            dock.style.width = Math.max(320, Math.min(saved.width, window.innerWidth - 32)) + "px";
            dock.style.height = Math.max(380, Math.min(saved.height, window.innerHeight - 130)) + "px";
         }
      } catch (e) { /* storage may be unavailable in a restricted browser */ }
   }

   function saveDockSize() {
      if (!dock || window.innerWidth <= 520 || dock.className !== "open") { return; }
      if (dockResizeTimer) { window.clearTimeout(dockResizeTimer); }
      dockResizeTimer = window.setTimeout(function () {
         try {
            var rect = dock.getBoundingClientRect();
            window.localStorage.setItem("curator.share.dock-size.v1", JSON.stringify({
               width: Math.round(rect.width), height: Math.round(rect.height)
            }));
         } catch (e) { /* storage may be unavailable in a restricted browser */ }
      }, 180);
   }

   function build() {
      if (document.getElementById("curator-launcher")) { return; }

      launcher = document.createElement("button");
      launcher.id = "curator-launcher";
      launcher.title = "Curator";
      launcher.innerHTML = "\u2756<span class='pip'></span>";

      dock = document.createElement("div");
      dock.id = "curator-dock";
      dock.setAttribute("aria-label", "Curator panel. Resize from the bottom-right corner.");
      frame = document.createElement("iframe");
      frame.title = "Curator";
      dock.appendChild(frame);

      document.body.appendChild(launcher);
      document.body.appendChild(dock);
      restoreDockSize();
      if (window.ResizeObserver) {
         dockResizeObserver = new ResizeObserver(saveDockSize);
         dockResizeObserver.observe(dock);
      }
      window.addEventListener("resize", restoreDockSize);
      launcher.onclick = toggle;
   }

   function toggle() {
      var opening = dock.className !== "open";
      dock.className = opening ? "open" : "";
      // Created on first open, so a page load does not pay for a service the
      // user may never touch.
      if (opening && !loaded) {
         frame.src = CURATOR_URL + "/bot";
         loaded = true;
      }
      if (opening) { watchContext(); } else { stopWatching(); }
   }

   /* --------------------------------------------------------- context */
   /* What the page can see, so "tag these" and "search here" resolve without
    * the user retyping what is already in front of them.
    *
    * Share exposes the document list as a registered component, which is a
    * supported contract and yields nodeRefs. Scraping the YUI table markup
    * would give the same names but break on any skin change. */
   var watcher = null, lastSignature = "";

   function documentList() {
      try {
         return Alfresco.util.ComponentManager.findFirst("Alfresco.DocumentList");
      } catch (e) {
         return null;
      }
   }

   function readContext() {
      var list = documentList(), selected = [], folderId = "", folderName = "";
      if (list) {
         try {
            var files = list.getSelectedFiles() || [];
            for (var i = 0; i < files.length && i < 200; i++) {
               var node = files[i].node || {};
               selected.push({
                  id: (node.nodeRef || "").split("/").pop(),
                  name: files[i].displayName || files[i].fileName || ""
               });
            }
         } catch (e) { /* an unselectable view, such as a details page */ }

         // doclistMetadata.parent is the folder actually being listed, which
         // currentPath is not: that is relative to the doclib root, so "/Finance"
         // in the repository browser is really "/Company Home/Finance" and
         // handing it over as a path would have the agent search the wrong place.
         try {
            var parent = (list.doclistMetadata || {}).parent || {};
            folderId = (parent.nodeRef || "").split("/").pop();
            folderName = (parent.properties || {})["cm:name"] || "";
         } catch (e) { /* no folder in this view */ }
      }
      return { folderId: folderId, folderName: folderName, selected: selected };
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
      // Selection changes raise Bubbling events, but not for every view, so
      // the page is sampled instead - and only while the panel is open.
      if (!watcher) { watcher = setInterval(pushContext, 1200); }
   }

   function stopWatching() {
      if (watcher) { clearInterval(watcher); watcher = null; }
   }

   function fetchTicketAndSend() {
      var request = new XMLHttpRequest();
      request.open("GET", TICKET_URL, true);
      request.setRequestHeader("Accept", "application/json");
      request.onreadystatechange = function () {
         if (request.readyState !== 4 || request.status !== 200) { return; }
         try {
            frame.contentWindow.postMessage({
               type: "curator:auth",
               ticket: JSON.parse(request.responseText).entry.id,
               baseUrl: ""
            }, ORIGIN);
         } catch (e) {
            // The panel tells the user to open the full workspace instead.
         }
      };
      request.send();
   }

   window.addEventListener("message", function (event) {
      if (event.origin !== ORIGIN || !event.data) { return; }

      if (event.data.type === "curator:expand") {
         window.location.href = Alfresco.constants.URL_PAGECONTEXT + "curator";
         return;
      }
      if (event.data.type === "curator:ready") {
         launcher.className = "ready";
         // Share has a Curator page, so expanding is a navigation here rather
         // than a new tab opened by the panel itself.
         frame.contentWindow.postMessage({ type: "curator:host", hasPage: true }, ORIGIN);
         lastSignature = "";
         pushContext();
         if (event.data.needsAuth) { fetchTicketAndSend(); }
      }
   }, false);

   if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", build);
   } else {
      build();
   }
})();
//]]></script>
</@>
