/*
 * Resolves the Curator service URL from share-config-custom.xml so an
 * administrator can repoint it without rebuilding the extension.
 */
function main() {
   var url = "http://localhost:8600";

   try {
      var scoped = config.scoped["Curator"];
      if (scoped) {
         var element = scoped["curator"];
         if (element) {
            var configured = element.getChildValue("url");
            if (configured) {
               // Concatenation forces a JavaScript string: Rhino would
               // otherwise resolve methods against java.lang.String, where
               // replace(regex, string) is ambiguous and throws.
               url = "" + configured;
            }
         }
      }
   } catch (e) {
      // Fall back to the default rather than breaking the page.
   }

   while (url.length > 1 && url.charAt(url.length - 1) === "/") {
      url = url.substring(0, url.length - 1);
   }

   model.curatorUrl = url;
   model.node = page.url.args.node ? "" + page.url.args.node : "";
   model.query = page.url.args.query ? "" + page.url.args.query : "";
}

main();
