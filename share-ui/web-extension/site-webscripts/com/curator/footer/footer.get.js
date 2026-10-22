/*
 * Resolves the Curator service URL for the floating launcher in the footer.
 *
 * Runs after the stock footer controller, so it only adds to the model.
 */
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
      // Fall back to the default rather than breaking every page's footer.
   }
   while (url.length > 1 && url.charAt(url.length - 1) === "/") {
      url = url.substring(0, url.length - 1);
   }
   model.curatorUrl = url;
})();
