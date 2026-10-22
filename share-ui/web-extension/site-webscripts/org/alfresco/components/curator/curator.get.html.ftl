<#assign curatorUrl = (curatorUrl!"http://localhost:8600")?html>
<#-- Charset is explicit: ?url alone fails unless url_escaping_charset is set. -->
<#assign nodeArg = (node!"")?url('UTF-8')>
<#assign queryArg = (query!"")?url('UTF-8')>

<div class="curator-page">
   <div id="curator-unavailable" class="curator-unavailable" style="display:none">
      <h2>${msg("curator.unavailable.title")}</h2>
      <p>${msg("curator.unavailable.detail")} <code>${curatorUrl}</code></p>
      <p class="curator-hint">${msg("curator.unavailable.hint")}</p>
   </div>
   <!-- The same document the floating panel loads. One assistant, two
        placements, so expanding the panel does not land somewhere else. -->
   <iframe id="curator-frame"
           class="curator-frame"
           title="${msg("curator.title")}"
           src="${curatorUrl}/bot<#if nodeArg?has_content>?node=${nodeArg}<#if queryArg?has_content>&amp;query=${queryArg}</#if><#elseif queryArg?has_content>?query=${queryArg}</#if>"></iframe>
</div>

<style type="text/css">
   /* Fill the space Share leaves between its header and footer. */
   .curator-page { padding: 0; margin: 0; }
   .curator-frame {
      display: block;
      width: 100%;
      height: calc(100vh - 150px);
      min-height: 480px;
      border: 1px solid #cccccc;
      border-radius: 4px;
      background: #ffffff;
   }
   .curator-unavailable {
      margin: 16px 0;
      padding: 18px 20px;
      background: #fdecea;
      border: 1px solid #e6a9a4;
      border-radius: 4px;
      color: #8c1d18;
   }
   .curator-unavailable h2 { margin: 0 0 6px; font-size: 15px; }
   .curator-unavailable p { margin: 4px 0; font-size: 13px; }
   .curator-unavailable code { font-family: Consolas, Monaco, monospace; }
   .curator-hint { color: #6a7581; }
</style>

<script type="text/javascript">//<![CDATA[
(function () {
   var CURATOR_ORIGIN = "${curatorUrl?js_string}";
   var TICKET_URL = Alfresco.constants.PROXY_URI_RELATIVE
      ? Alfresco.constants.URL_CONTEXT + "proxy/alfresco-api/-default-/public/authentication/versions/1/tickets/-me-"
      : "/share/proxy/alfresco-api/-default-/public/authentication/versions/1/tickets/-me-";

   var frame = document.getElementById("curator-frame");
   var unavailable = document.getElementById("curator-unavailable");
   var origin = CURATOR_ORIGIN.replace(/^(https?:\/\/[^\/]+).*$/, "$1");
   var handshakeDone = false;

   // The handshake doubles as a reachability check: a curator that never
   // announces itself is either down or blocked, and the user is told so.
   var timer = window.setTimeout(function () {
      if (!handshakeDone) {
         frame.style.display = "none";
         unavailable.style.display = "block";
      }
   }, 10000);

   function sendTicket(ticket) {
      frame.contentWindow.postMessage({
         type: "curator:auth",
         ticket: ticket,
         baseUrl: "",
         origin: window.location.origin
      }, origin);
   }

   function fetchTicketAndSend() {
      var request = new XMLHttpRequest();
      request.open("GET", TICKET_URL, true);
      request.setRequestHeader("Accept", "application/json");
      request.onreadystatechange = function () {
         if (request.readyState !== 4) { return; }
         if (request.status === 200) {
            try {
               sendTicket(JSON.parse(request.responseText).entry.id);
            } catch (e) {
               // The panel falls back to its own sign-in form.
            }
         }
      };
      request.send();
   }

   window.addEventListener("message", function (event) {
      if (event.origin !== origin) { return; }
      if (!event.data || event.data.type !== "curator:ready") { return; }

      // "ready" proves the panel loaded, independently of whether it needs a
      // ticket: a panel that already holds a session sends it too.
      handshakeDone = true;
      window.clearTimeout(timer);

      if (event.data.needsAuth) {
         fetchTicketAndSend();
      }
   }, false);
}());
//]]></script>
