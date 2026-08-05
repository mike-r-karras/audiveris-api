from __future__ import annotations

from io import BytesIO
from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET


CONTAINER_PATH = "META-INF/container.xml"
CONTAINER_NAMESPACE = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container"
}


class MusicXMLLoadError(ValueError):
    pass


def load_musicxml(source: str | Path | bytes) -> bytes:
    """
    Load either:
      - uncompressed .xml / .musicxml
      - compressed .mxl

    Returns the uncompressed MusicXML document as bytes.
    """
    if isinstance(source, bytes):
        data = source
        filename = None
    else:
        path = Path(source)
        data = path.read_bytes()
        filename = path.name

    # ZIP files begin with PK. This also avoids depending only on extensions.
    if data.startswith(b"PK"):
        return _extract_musicxml_from_mxl(data, filename)

    _validate_xml(data, filename)
    return data


def _extract_musicxml_from_mxl(
    archive_data: bytes,
    filename: str | None = None,
) -> bytes:
    try:
        with zipfile.ZipFile(BytesIO(archive_data)) as archive:
            rootfile_path = _find_rootfile(archive)

            try:
                musicxml_data = archive.read(rootfile_path)
            except KeyError as exc:
                raise MusicXMLLoadError(
                    f"MXL rootfile does not exist in archive: {rootfile_path}"
                ) from exc

    except zipfile.BadZipFile as exc:
        raise MusicXMLLoadError(
            f"Invalid compressed MusicXML file: {filename or 'uploaded file'}"
        ) from exc

    _validate_xml(musicxml_data, rootfile_path)
    return musicxml_data


def _find_rootfile(archive: zipfile.ZipFile) -> str:
    """
    Read META-INF/container.xml when available.

    Falls back to finding a likely MusicXML file because some producers create
    technically incomplete MXL archives.
    """
    try:
        container_data = archive.read(CONTAINER_PATH)
    except KeyError:
        return _find_xml_fallback(archive)

    try:
        container_root = ET.fromstring(container_data)
    except ET.ParseError as exc:
        raise MusicXMLLoadError(
            "Invalid META-INF/container.xml in MXL archive"
        ) from exc

    # Handle both namespaced and non-namespaced container.xml files.
    rootfile = container_root.find(
        ".//container:rootfile",
        CONTAINER_NAMESPACE,
    )

    if rootfile is None:
        rootfile = container_root.find(".//rootfile")

    if rootfile is None:
        return _find_xml_fallback(archive)

    full_path = rootfile.get("full-path")

    if not full_path:
        raise MusicXMLLoadError(
            "MXL container rootfile is missing its full-path attribute"
        )

    return full_path


def _find_xml_fallback(archive: zipfile.ZipFile) -> str:
    candidates = [
        name
        for name in archive.namelist()
        if not name.endswith("/")
        and not name.upper().startswith("META-INF/")
        and name.lower().endswith((".xml", ".musicxml"))
    ]

    if not candidates:
        raise MusicXMLLoadError(
            "No MusicXML document was found inside the MXL archive"
        )

    return candidates[0]


def _validate_xml(data: bytes, filename: str | None = None) -> None:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise MusicXMLLoadError(
            f"Invalid MusicXML in {filename or 'uploaded file'}: {exc}"
        ) from exc

    tag = root.tag.rsplit("}", 1)[-1]

    if tag not in {"score-partwise", "score-timewise"}:
        raise MusicXMLLoadError(
            f"Unexpected MusicXML root element <{tag}> "
            f"in {filename or 'uploaded file'}"
        )