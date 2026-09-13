"""One read-only Poppler operation with strict process resource limits.

Invoked by pdf_backend using an already-open regular file descriptor. No GTK,
external link actions, JavaScript, uploads, attachments or document writes.
"""
import json
import math
import os
from pathlib import Path
import resource
import sys

MAX_PIXELS = 8_000_000
MAX_DIMENSION = 4096


def render_geometry(width, height, viewport_width, viewport_height, zoom):
    if not all(math.isfinite(value) for value in (width, height, zoom)) or not 1 <= width <= 14400 or not 1 <= height <= 14400:
        raise ValueError("geometry")
    viewport_width, viewport_height = max(64, min(2048, int(viewport_width))), max(64, min(2048, int(viewport_height)))
    zoom = max(0.5, min(4.0, float(zoom)))
    scale = min(viewport_width / width, viewport_height / height) * zoom
    scale = min(scale, MAX_DIMENSION / width, MAX_DIMENSION / height, math.sqrt(MAX_PIXELS / (width * height)))
    return max(1, int(width * scale)), max(1, int(height * scale)), scale


def operation(spec, root):
    try:
        import gi
        gi.require_version("Poppler", "0.18")
        gi.require_foreign("cairo")
        from gi.repository import GLib, Poppler
        import cairo
    except (ImportError, ValueError):
        return {"error": "dependency"}
    descriptor = int(spec["fd"])
    if os.pread(descriptor, 5, 0) != b"%PDF-":
        return {"error": "invalid"}
    try:
        document = Poppler.Document.new_from_file(Path(f"/proc/self/fd/{descriptor}").as_uri(), None)
    except GLib.Error as exc:
        return {"error": "encrypted" if exc.matches(Poppler.error_quark(), Poppler.Error.ENCRYPTED) else "invalid"}
    count = document.get_n_pages()
    if not 0 < count <= 1000:
        return {"error": "pages"}
    if spec["action"] == "metadata":
        page = document.get_page(0)
        width, height, scale = render_geometry(*page.get_size(), 256, 256, 1)
        # Validate native page rendering before the UI replaces the old book.
        surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
        context = cairo.Context(surface)
        context.scale(scale, scale)
        page.render(context)
        return {"pages": count, "title": (document.get_title() or "")[:200]}
    index = max(0, min(count - 1, int(spec.get("page", 0))))
    if spec["action"] == "search":
        query, after = spec["query"], int(spec.get("after", -1))
        if not 0 < len(query) <= 128:
            return {"error": "invalid"}
        for distance in range(count + 1):
            current = (index + distance) % count
            page = document.get_page(current)
            rectangles = page.find_text(query)
            start = after + 1 if distance == 0 else 0
            end = min(len(rectangles), after + 1) if distance == count else len(rectangles)
            if start < end:
                rectangle = rectangles[start]
                return {"match": {"page": current, "index": start, "rect": [rectangle.x1, rectangle.y1, rectangle.x2, rectangle.y2], "wrapped": index + distance >= count}}
        return {"match": None}
    if spec["action"] != "render":
        return {"error": "invalid"}
    page = document.get_page(index)
    page_width, page_height = page.get_size()
    width, height, scale = render_geometry(page_width, page_height, spec["width"], spec["height"], spec["zoom"])
    surface = cairo.ImageSurface(cairo.FORMAT_RGB24, width, height)
    context = cairo.Context(surface)
    context.set_source_rgb(1, 1, 1)
    context.paint()
    context.scale(scale, scale)
    page.render(context)
    match = spec.get("match")
    if match:
        x1, y1, x2, y2 = match
        if all(math.isfinite(value) for value in match):
            context.set_source_rgba(1, 0.75, 0, 0.38)
            context.rectangle(x1, page_height - y2, x2 - x1, y2 - y1)
            context.fill()
    surface.write_to_png(str(root / "page.png"))
    return {"width": width, "height": height, "scale": scale, "pageHeight": page_height}


def main():
    # Limits precede native imports and parsing. A canceled worker is disposable.
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (25, 26))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024**2, 32 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    root = Path(sys.argv[1])
    try:
        spec = json.loads((root / "request.json").read_text())
        result = operation(spec, root)
    except MemoryError:
        result = {"error": "resource"}
    except ValueError as exc:
        result = {"error": "geometry" if str(exc) == "geometry" else "invalid"}
    except Exception:
        result = {"error": "invalid"}
    (root / "result.json").write_text(json.dumps(result))


if __name__ == "__main__":
    main()
