# Share with the Curator built in.
#
# Layered onto the stock Share image rather than rebuilt from the
# enterprise-share sources. The customisation is only Surf resources and one
# config block, whereas a source build needs a JDK, Maven, a clone and
# credentials for artifacts.alfresco.com - none of which belong in
# `docker compose up`. The stock image also tracks the 26.3 line, so it matches
# the repository rather than trailing it.
ARG SHARE_BASE=quay.io/alfresco/alfresco-share:latest
FROM ${SHARE_BASE}

ARG EXT=/usr/local/tomcat/shared/classes/alfresco/web-extension

# The Surf page, template, component webscript, header item and footer launcher.
COPY share-ui/web-extension/ ${EXT}/
COPY share-ui/config/curator-share-config.xml /tmp/curator-share-config.xml
COPY docker/share/curator-substituter.sh /usr/local/tomcat/shared/classes/alfresco/curator-substituter.sh

RUN set -eu; \
    config="${EXT}/share-config-custom.xml"; \
    # Drop the file's explanatory header; only the marked block is config.
    sed -n '/<!-- CURATOR:BEGIN -->/,$p' /tmp/curator-share-config.xml \
      | sed 's|{curatorUrl}|CURATOR_URL|g' > /tmp/curator-block.xml; \
    # Insert before the closing tag. Appending after it would be ignored, and
    # sed's `r` can only place text *after* a matching line.
    sed -i 's|</alfresco-config>||' "$config"; \
    cat /tmp/curator-block.xml >> "$config"; \
    printf '</alfresco-config>\n' >> "$config"; \
    rm -f /tmp/curator-share-config.xml /tmp/curator-block.xml; \
    chmod +x /usr/local/tomcat/shared/classes/alfresco/curator-substituter.sh; \
    grep -q 'CURATOR:BEGIN' "$config"

ENTRYPOINT ["/usr/local/tomcat/shared/classes/alfresco/curator-substituter.sh", "catalina.sh run"]
