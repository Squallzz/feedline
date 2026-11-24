#!/usr/bin/env python3
"""Put Feedline's icons into feedline.html: the Lucide icons listed in NAMES, as one inline SVG sprite (a <symbol> per icon)
between the <!-- icons:start --> and <!-- icons:end --> markers, with Lucide's licence. Inline, so the page needs no
internet and stays one file. The page shows an icon with ico("name") in script, or <svg class="i"><use href="#i-name"/></svg>.

To add or change icons: edit NAMES, then
    npm pack lucide-static@<version> && tar -xzf lucide-static-<version>.tgz
    python3 tools/build_icons.py package        # the unpacked lucide-static folder
Only plain shapes (path, rect, circle, line, polyline, polygon, ellipse) are copied; anything else stops the build.
"""
import json
import os
import re
import sys

NAMES = """house layout-grid list chart-column layers square-terminal folder zap settings disc-3 move sliders-horizontal
pause play x octagon-x arrow-up arrow-down arrow-left arrow-right chevrons-up chevrons-down power-off power
arrow-down-to-line arrow-up-from-line rotate-ccw rotate-cw snowflake flame eject square-parking hand-grab repeat nfc
triangle-alert cpu download delete save undo-2 refresh-cw send""".split()
SHAPES = {"path", "rect", "circle", "line", "polyline", "polygon", "ellipse"}
HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "feedline.html")


def symbol(pkg, name):
    with open(os.path.join(pkg, "icons", name + ".svg"), encoding="utf-8") as f:
        body = re.search(r"<svg[^>]*>(.*)</svg>", f.read(), re.S).group(1)
    elems = re.findall(r"<[^>]+>", body)
    if not elems or any(not re.fullmatch(r"<(%s)\b[^<>]*/>" % "|".join(SHAPES), e) or re.search(r"(?i)\bon\w+\s*=|href|javascript:", e) for e in elems):
        sys.exit("unexpected markup in %s.svg" % name)
    return '<symbol id="i-%s" viewBox="0 0 24 24">%s</symbol>' % (name, "".join(re.sub(r"\s+", " ", e).replace(" />", "/>") for e in elems))


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    pkg = sys.argv[1]
    with open(os.path.join(pkg, "package.json"), encoding="utf-8") as f:
        version = json.load(f)["version"]
    with open(os.path.join(pkg, "LICENSE"), encoding="utf-8") as f:
        licence = f.read().strip().replace("--", "- -")          # "--" may not appear inside an HTML comment
    block = ("<!-- icons:start (made by tools/build_icons.py from lucide-static %s, https://lucide.dev: edit NAMES there, not here)\n\n%s\n-->\n"
             '<svg id="icons" width="0" height="0" style="position:absolute" aria-hidden="true">\n%s\n</svg>\n<!-- icons:end -->'
             % (version, licence, "\n".join(symbol(pkg, n) for n in NAMES)))
    with open(HTML, encoding="utf-8", newline="") as f:
        page = f.read()
    if "\r\n" in page:                                   # keep the file's own line endings
        block = block.replace("\r\n", "\n").replace("\n", "\r\n")
    new, n = re.subn(r"<!-- icons:start.*?<!-- icons:end -->", lambda m: block, page, flags=re.S)
    if n != 1:
        sys.exit("feedline.html needs exactly one <!-- icons:start --> ... <!-- icons:end --> block")
    with open(HTML, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    print("feedline.html: %d icons from lucide-static %s" % (len(NAMES), version))


if __name__ == "__main__":
    main()
