# SPDX-License-Identifier: AGPL-3.0-or-later
"""Minimal, read-only UPnP ContentDirectory client for USB media browsing.

The CXN100 exposes its USB library as a UPnP AV ContentDirectory service on
port 8050 under /<udn>/ContentDirectory/invoke. This module uses only the
Python standard library and performs no device writes.
"""

from __future__ import annotations

import html
import http.client
import json
import re
import socket
import time
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote, urlencode

DIDL_NS = "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
DC_NS = "http://purl.org/dc/elements/1.1/"
UPNP_NS = "urn:schemas-upnp-org:metadata-1-0/upnp/"
SOAP_ACTION = "urn:schemas-upnp-org:service:ContentDirectory:1#Browse"

for _prefix, _uri in (("", DIDL_NS), ("dc", DC_NS), ("upnp", UPNP_NS)):
    ET.register_namespace(_prefix, _uri)


class UPnPError(RuntimeError):
    """Raised when the ContentDirectory request fails."""


_ID_RE = re.compile(r"^[A-Za-z0-9_:.\-]{1,160}$")
_DURATION_RE = re.compile(r"^\d+:\d{2}:\d{2}(?:\.\d+)?$")
_ARTWORK_NAMES = re.compile(
    r"^(?:@?eaDir|@?tmp|album\s?art|art|artwork|cover|covers|"
    r"cd\s?art|cd\s?artwork|digital\s?booklet|booklet|scans?|extras?|"
    r"albumart)$",
    re.IGNORECASE,
)
_ALLOWED_CLASSES = ("object.item.audioItem", "object.container")


def _esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _validate_item(item: dict[str, Any]) -> dict[str, str]:
    """Validate the small set of fields needed to rebuild a DIDL object."""
    object_id = str(item.get("id") or "").strip()
    parent_id = str(item.get("parent_id") or item.get("parentID") or "")
    item_class = str(item.get("class") or "").strip()
    title = str(item.get("title") or "").strip()
    if not _ID_RE.fullmatch(object_id):
        raise UPnPError("Invalid library object id")
    if parent_id and not _ID_RE.fullmatch(parent_id):
        raise UPnPError("Invalid library parent id")
    if not item_class.startswith(_ALLOWED_CLASSES):
        raise UPnPError("Unsupported library object type")
    if not title or len(title) > 300:
        raise UPnPError("Invalid library title")
    resource = str(item.get("resource") or "").strip()
    if resource and not resource.startswith("file://"):
        raise UPnPError("Unsupported library resource")
    art_url = str(item.get("art_url") or item.get("album_art_uri") or "").strip()
    if art_url and not art_url.lower().startswith(("http://", "https://")):
        raise UPnPError("Unsupported album art URL")
    duration = str(item.get("duration") or "").strip()
    if duration and not _DURATION_RE.fullmatch(duration):
        raise UPnPError("Invalid track duration")
    return {
        "id": object_id,
        "parent_id": parent_id,
        "class": item_class,
        "title": title,
        "artist": str(item.get("artist") or "")[:300],
        "album": str(item.get("album") or "")[:300],
        "track": str(item.get("track") or "")[:16],
        "duration": duration,
        "resource": resource,
        "art_url": art_url,
    }


def normalize_item(item: dict[str, Any]) -> dict[str, str]:
    """Public wrapper used by the controller before a queue command."""
    return _validate_item(item)


def didl_envelope(fragment: str) -> str:
    """Wrap one DIDL item/container in the envelope the CXN100 expects."""
    return (
        "<DIDL-Lite xmlns='urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/' "
        "xmlns:dc='http://purl.org/dc/elements/1.1/' "
        "xmlns:upnp='urn:schemas-upnp-org:metadata-1-0/upnp/'>"
        + fragment
        + "</DIDL-Lite>"
    )


def media_didl(item: dict[str, Any]) -> str:
    """Build a minimal DIDL object from previously browsed, validated metadata."""
    clean = _validate_item(item)
    attributes = (
        f"id='{_esc(clean['id'])}' parentID='{_esc(clean['parent_id'])}' restricted='true'"
    )
    lines = [f"<upnp:class>{_esc(clean['class'])}</upnp:class>",
             f"<dc:title>{_esc(clean['title'])}</dc:title>"]
    if clean["class"].startswith("object.item.audioItem"):
        if clean["artist"]:
            lines.append(f"<upnp:artist>{_esc(clean['artist'])}</upnp:artist>")
        if clean["album"]:
            lines.append(f"<upnp:album>{_esc(clean['album'])}</upnp:album>")
        if clean["track"]:
            lines.append(f"<upnp:originalTrackNumber>{_esc(clean['track'])}</upnp:originalTrackNumber>")
        if clean["resource"]:
            lines.append(
                f"<res duration='{_esc(clean['duration'])}' protocolInfo='*:*:*:*'>"
                f"{_esc(clean['resource'])}</res>"
            )
    if clean["art_url"]:
        lines.append(f"<upnp:albumArtURI>{_esc(clean['art_url'])}</upnp:albumArtURI>")
    tag = "container" if clean["class"].startswith("object.container") else "item"
    return didl_envelope(f"<{tag} {attributes}>\n" + "\n".join(lines) + f"\n</{tag}>\n")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str:
    for child in element.iter():
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return ""


def _browse_xml(object_id: str, flag: str, start: int, count: int) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" '
        'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
        "<s:Body>"
        '<u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
        f"<ObjectID>{object_id}</ObjectID>"
        f"<BrowseFlag>{flag}</BrowseFlag>"
        "<Filter>*</Filter>"
        f"<StartingIndex>{start}</StartingIndex>"
        f"<RequestedCount>{count}</RequestedCount>"
        "<SortCriteria></SortCriteria>"
        "</u:Browse>"
        "</s:Body>"
        "</s:Envelope>"
    ).encode("utf-8")


def _soap_request(
    host: str,
    port: int,
    path: str,
    body: bytes,
    timeout: float,
) -> bytes:
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Host": f"{host}:{port}",
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPACTION": f'"{SOAP_ACTION}"',
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        raw = response.read()
        if response.status != 200:
            raise UPnPError(f"ContentDirectory returned HTTP {response.status}")
        return raw
    except (OSError, socket.timeout) as exc:
        raise UPnPError(f"Cannot reach UPnP service at {host}:{port}: {exc}") from exc
    finally:
        connection.close()


def _parse_didl(didl_xml: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    try:
        didl = ET.fromstring(didl_xml)
    except ET.ParseError as exc:
        raise UPnPError(f"Invalid DIDL response: {exc}") from exc

    items: list[dict[str, Any]] = []
    for element in didl:
        tag = _local(element.tag)
        if tag not in ("item", "container"):
            continue
        resource = next(
            (child for child in element.iter() if _local(child.tag) == "res"),
            None,
        )
        item = {
            "id": element.get("id", ""),
            "parent_id": element.get("parentID", ""),
            "restricted": element.get("restricted", ""),
            "class": _child_text(element, "class"),
            "title": _child_text(element, "title"),
            "artist": _child_text(element, "artist"),
            "album": _child_text(element, "album"),
            "genre": _child_text(element, "genre"),
            "track": _child_text(element, "originalTrackNumber"),
            "duration": resource.get("duration", "") if resource is not None else "",
            "resource": (resource.text or "").strip() if resource is not None else "",
            "art_url": _child_text(element, "albumArtURI"),
            "is_container": tag == "container",
        }
        try:
            item["didl"] = media_didl(item)
        except UPnPError:
            item["didl"] = didl_envelope(ET.tostring(element, encoding="unicode"))
        items.append(item)

    numbers: dict[str, int] = {}
    for name in ("NumberReturned", "TotalMatches", "UpdateID"):
        numbers[name] = 0
    return items, numbers


def browse(
    host: str,
    udn: str,
    object_id: str = "0",
    flag: str = "BrowseDirectChildren",
    start: int = 0,
    count: int = 100,
    port: int = 8050,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Browse a ContentDirectory object. Always read-only."""
    if flag not in ("BrowseDirectChildren", "BrowseMetadata"):
        raise UPnPError("Unsupported browse flag")
    path = f"/{udn}/ContentDirectory/invoke"
    body = _browse_xml(object_id, flag, start, count)
    for attempt in range(3):
        try:
            raw = _soap_request(host, port, path, body, timeout)
            break
        except UPnPError as exc:
            # The player's ContentDirectory occasionally returns a transient
            # HTTP 500 while it re-indexes the USB drive. Retry briefly.
            if attempt == 2 or "HTTP 500" not in str(exc):
                raise
            time.sleep(0.25 * (attempt + 1))
    try:
        envelope = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise UPnPError(f"Invalid SOAP response: {exc}") from exc

    result_xml = ""
    for element in envelope.iter():
        if _local(element.tag) == "Result":
            result_xml = element.text or ""
            break
    else:
        raise UPnPError("SOAP response has no Result")

    items, numbers = _parse_didl(result_xml)
    for element in envelope.iter():
        name = _local(element.tag)
        if name in numbers and element.text:
            try:
                numbers[name] = int(element.text.strip())
            except ValueError:
                pass
    return {
        "items": items,
        "object_id": object_id,
        "number_returned": numbers["NumberReturned"],
        "total": numbers["TotalMatches"],
        "update_id": numbers["UpdateID"],
    }


def is_empty_artwork_folder(
    host: str,
    udn: str,
    item: dict[str, Any],
    port: int = 8050,
    timeout: float = 4.0,
) -> bool:
    """Hide sidecar artwork folders only when they contain no media at all."""
    if not item.get("is_container"):
        return False
    title = str(item.get("title") or "").strip()
    if not _ARTWORK_NAMES.fullmatch(title):
        return False
    if str(item.get("class") or "").startswith("object.container.album"):
        return False
    try:
        children = browse(
            host,
            udn,
            object_id=str(item.get("id") or ""),
            start=0,
            count=1,
            port=port,
            timeout=timeout,
        )
    except UPnPError:
        return False
    return not children.get("items") and int(children.get("total", 0)) == 0


def queue_add(
    host: str,
    port: int,
    udn: str,
    didl: str,
    action: str = "APPEND",
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Append, play now, or insert a DIDL object after the current track."""
    if action not in ("APPEND", "PLAY_NOW", "PLAY_NEXT"):
        raise UPnPError("Unsupported queue action")
    params = urlencode(
        {"action": action, "didl": didl, "server_udn": udn},
        quote_via=quote,
    )
    path = f"/smoip/queue/add?{params}"
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Host": f"{host}:{port}", "Connection": "close"})
        response = connection.getresponse()
        raw = response.read()
        if response.status != 200:
            raise UPnPError(f"/queue/add returned HTTP {response.status}")
        if not raw:
            return {"ok": True}
        return json.loads(raw.decode("utf-8"))
    except (OSError, socket.timeout) as exc:
        raise UPnPError(f"Cannot reach {host}:{port}: {exc}") from exc
    finally:
        connection.close()
