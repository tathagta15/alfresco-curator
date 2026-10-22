#!/bin/sh
# Resolve the browser-facing Curator address, then hand over to the stock
# Share entrypoint. Wrapping it leaves the vendor script untouched, so a base
# image upgrade cannot silently drop an edit made inside it.
set -e

: "${CURATOR_URL:=http://localhost:8600}"
CONFIG=/usr/local/tomcat/shared/classes/alfresco/web-extension/share-config-custom.xml

echo "Replace 'CURATOR_URL' with '$CURATOR_URL'"
sed -i -e "s|CURATOR_URL|$CURATOR_URL|g" "$CONFIG"

exec /usr/local/tomcat/shared/classes/alfresco/substituter.sh "$@"
