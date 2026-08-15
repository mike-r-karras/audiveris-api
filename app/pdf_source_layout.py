"""Lossless word-geometry extraction for chord-chart PDF processing."""

from __future__ import annotations

from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET


class PdfLayoutExtractionError(RuntimeError):
    pass


def extract_pdf_source_layout(path: str | Path) -> dict[str, object]:
    """Return page, line, and word geometry without interpreting the music."""
    pdf_path = Path(path)
    try:
        completed = subprocess.run(
            ["pdftotext", "-bbox-layout", str(pdf_path), "-"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise PdfLayoutExtractionError("pdftotext is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise PdfLayoutExtractionError("PDF layout extraction timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise PdfLayoutExtractionError(
            f"PDF layout extraction failed{': ' + detail if detail else ''}"
        ) from exc

    return parse_bbox_layout(
        completed.stdout,
        source_filename=pdf_path.name,
    )


def parse_bbox_layout(
    bbox_xml: str | bytes,
    *,
    source_filename: str,
) -> dict[str, object]:
    """Parse Poppler ``-bbox-layout`` XHTML into stable source identities."""
    try:
        root = ET.fromstring(bbox_xml)
    except ET.ParseError as exc:
        raise PdfLayoutExtractionError(
            f"Invalid pdftotext bbox output: {exc}"
        ) from exc

    metadata = {
        element.get("name"): element.get("content", "")
        for element in root.iter()
        if _local_name(element.tag) == "meta" and element.get("name")
    }

    pages: list[dict[str, object]] = []
    for page_index, page_element in enumerate(
        (element for element in root.iter() if _local_name(element.tag) == "page"),
        start=1,
    ):
        words: list[dict[str, object]] = []
        lines: list[dict[str, object]] = []

        for line_index, line_element in enumerate(
            (
                element
                for element in page_element.iter()
                if _local_name(element.tag) == "line"
            ),
            start=1,
        ):
            word_ids: list[str] = []
            line_text: list[str] = []
            for word_element in (
                element
                for element in line_element
                if _local_name(element.tag) == "word"
            ):
                word_id = f"p{page_index}-w{len(words) + 1}"
                text = "".join(word_element.itertext())
                word_ids.append(word_id)
                line_text.append(text)
                words.append(
                    {
                        "id": word_id,
                        "text": text,
                        "box": _box(word_element),
                    }
                )

            lines.append(
                {
                    "id": f"p{page_index}-l{line_index}",
                    "text": " ".join(line_text),
                    "wordIds": word_ids,
                    "box": _box(line_element),
                }
            )

        pages.append(
            {
                "number": page_index,
                "width": _float_attribute(page_element, "width"),
                "height": _float_attribute(page_element, "height"),
                "lines": lines,
                "words": words,
                # Raster-derived regions will be appended by a separate visual
                # detector without changing the immutable word geometry.
                "visualRegions": [],
            }
        )

    if not pages:
        raise PdfLayoutExtractionError("pdftotext returned no PDF pages")

    return {
        "schemaVersion": "pdf-source-layout-1.0",
        "sourceFormat": "pdf",
        "sourceFilename": source_filename,
        "extractor": {
            "name": "poppler-pdftotext",
            "mode": "bbox-layout",
        },
        "metadata": metadata,
        "pages": pages,
        "warnings": [],
    }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _float_attribute(element: ET.Element, name: str) -> float:
    value = element.get(name)
    if value is None:
        raise PdfLayoutExtractionError(f'bbox element is missing "{name}"')
    try:
        return float(value)
    except ValueError as exc:
        raise PdfLayoutExtractionError(
            f'bbox attribute "{name}" is not numeric: {value}'
        ) from exc


def _box(element: ET.Element) -> dict[str, float]:
    return {
        "xMin": _float_attribute(element, "xMin"),
        "yMin": _float_attribute(element, "yMin"),
        "xMax": _float_attribute(element, "xMax"),
        "yMax": _float_attribute(element, "yMax"),
    }
