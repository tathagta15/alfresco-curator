"""Node media proxied on the session's behalf.

The browser holds no Alfresco credentials, so a thumbnail cannot be linked
directly from the repository; it is fetched with the session's ticket and
relayed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response, status

from alfresco_curator.alfresco import afts
from alfresco_curator.errors import AlfrescoError
from alfresco_curator.web.dependencies import Searcher

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/nodes", tags=["nodes"])

# Renditions are generated asynchronously, so a miss is normal for new content
# and the UI falls back to a file-type icon.
NOT_AVAILABLE = Response(status_code=status.HTTP_404_NOT_FOUND)


@router.get(
    "/{node_id}/thumbnail",
    responses={200: {"content": {"image/*": {}}}, 404: {"description": "No rendition yet"}},
)
def thumbnail(node_id: str, client: Searcher) -> Response:
    try:
        afts.node_ref(node_id)  # rejects traversal and injection before any call
    except ValueError:
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    try:
        # Renditions are made on demand: ask once, then serve the placeholder
        # so the row is never empty, and the real image appears next time.
        if client.rendition_status(node_id) != "CREATED":
            client.request_rendition(node_id)
        content, content_type = client.get_rendition(node_id)
    except AlfrescoError:
        return NOT_AVAILABLE

    return Response(
        content=content,
        media_type=content_type,
        # Private: the image is only valid for this user's permissions.
        headers={"Cache-Control": "private, max-age=60"},
    )
