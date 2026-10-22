/*
 * Share header customisation: adds "Curator" to the application menu,
 * and resolves the service URL for the floating assistant in head.ftl.
 *
 * Runs after the stock share-header.get.js, so it appends to the model the
 * base controller has already built rather than replacing it.
 */
var appMenuBar = widgetUtils.findObject(model.jsonModel.widgets, "id", "HEADER_APP_MENU_BAR");

if (appMenuBar && appMenuBar.config && appMenuBar.config.widgets) {
   appMenuBar.config.widgets.push({
      id: "HEADER_CURATOR",
      name: "alfresco/menus/AlfMenuBarItem",
      config: {
         id: "HEADER_CURATOR",
         label: "header.menu.curator.label",
         targetUrl: "curator",
         targetUrlType: "PAGE_RELATIVE"
      }
   });
}

(function resolveCuratorUrl() {
   var url = "http://localhost:8600";
   try {
      var scoped = config.scoped["Curator"];
      if (scoped) {
         var element = scoped["curator"];
         if (element) {
            var configured = element.getChildValue("url");
            // Concatenation forces a JavaScript string: Rhino would otherwise
            // resolve methods against java.lang.String, where replace is
            // ambiguous and throws.
            if (configured) { url = "" + configured; }
         }
      }
   } catch (e) {
      // Fall back to the default rather than breaking every page's header.
   }
   while (url.length > 1 && url.charAt(url.length - 1) === "/") {
      url = url.substring(0, url.length - 1);
   }
   model.curatorUrl = url;
})();
