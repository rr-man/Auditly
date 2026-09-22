#!/usr/bin/env python3
"""A minimal PDF writer: enough for a paginated scorecard, nothing more.

Hand-rolled for the same reason the Call Flow Builder hand-rolls its xlsx and
ZIP -- this project takes no dependencies, and a transcript needs only Helvetica
text in one column.

Supports: page breaks, bold/regular, simple word wrap, WinAnsi text. It does not
do images, tables, unicode beyond Latin-1, or embedded fonts. If a future export
needs those, use a library rather than growing this.
"""

PAGE_W, PAGE_H = 612, 792                 # US Letter, points
MARGIN_X, MARGIN_TOP, MARGIN_BOT = 54, 54, 54

# Helvetica advance widths (units/1000) for the printable Latin-1 range. Only
# used for wrapping, so approximation beyond ASCII is acceptable.
_W = {}
for _c in range(32, 256):
    _W[_c] = 556
for _c, _w in {32: 278, 33: 278, 34: 355, 39: 191, 40: 333, 41: 333, 44: 278,
               45: 333, 46: 278, 47: 278, 58: 278, 59: 278, 73: 278, 74: 500,
               102: 278, 105: 222, 106: 222, 107: 500, 108: 222, 109: 833,
               114: 333, 116: 278, 119: 722, 122: 500}.items():
    _W[_c] = _w
for _c in range(97, 123):
    _W.setdefault(_c, 556)
for _c in range(48, 58):
    _W[_c] = 556


def _text_width(s, size):
    return sum(_W.get(ord(ch), 556) for ch in s) * size / 1000.0


def _esc(s):
    out = []
    for ch in s:
        o = ord(ch)
        if ch in "()\\":
            out.append("\\" + ch)
        elif 32 <= o < 127:
            out.append(ch)
        elif o < 256:
            out.append("\\%03o" % o)
        else:
            out.append("?")            # outside WinAnsi; wrong to guess a glyph
    return "".join(out)


def wrap(text, size, width):
    """Greedy word wrap to `width` points. Never returns an empty list."""
    lines, cur = [], ""
    for word in (text or "").split():
        trial = (cur + " " + word).strip()
        if _text_width(trial, size) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    # a single word longer than the line still has to break somewhere
    out = []
    for ln in lines:
        while _text_width(ln, size) > width and len(ln) > 1:
            cut = len(ln)
            while cut > 1 and _text_width(ln[:cut], size) > width:
                cut -= 1
            out.append(ln[:cut])
            ln = ln[cut:]
        out.append(ln)
    return out or [""]


class Pdf:
    def __init__(self, title="Document"):
        self.title = title
        self.pages = []
        self._new_page()

    def _new_page(self):
        self.ops = []
        self.y = PAGE_H - MARGIN_TOP
        self.pages.append(self.ops)

    def space(self, pts):
        self.y -= pts

    def rule(self):
        self._room(12)
        self.ops.append("0.80 0.80 0.80 RG 0.6 w %s %s m %s %s l S"
                        % (MARGIN_X, round(self.y, 1),
                           PAGE_W - MARGIN_X, round(self.y, 1)))
        self.y -= 12

    def _room(self, need):
        if self.y - need < MARGIN_BOT:
            self._new_page()

    def text(self, s, size=10, bold=False, indent=0, leading=None,
             color=(0, 0, 0), gap_after=0):
        lead = leading or size * 1.35
        width = PAGE_W - 2 * MARGIN_X - indent
        for line in wrap(s, size, width):
            self._room(lead)
            self.ops.append(
                "BT /%s %s Tf %s %s %s rg %s %s Td (%s) Tj ET"
                % ("F2" if bold else "F1", size, color[0], color[1], color[2],
                   MARGIN_X + indent, round(self.y - size, 1), _esc(line)))
            self.y -= lead
        self.y -= gap_after

    def build(self):
        objs, page_ids = [], []
        n_pages = len(self.pages)
        # 1 catalog, 2 pages tree, 3 F1, 4 F2, then per page: page + content
        for i in range(n_pages):
            page_ids.append(5 + i * 2)

        objs.append((1, "<< /Type /Catalog /Pages 2 0 R >>"))
        kids = " ".join("%d 0 R" % p for p in page_ids)
        objs.append((2, "<< /Type /Pages /Count %d /Kids [%s] >>" % (n_pages, kids)))
        objs.append((3, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                        "/Encoding /WinAnsiEncoding >>"))
        objs.append((4, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                        "/Encoding /WinAnsiEncoding >>"))
        for i, ops in enumerate(self.pages):
            pid, cid = page_ids[i], page_ids[i] + 1
            objs.append((pid,
                         "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
                         "/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
                         "/Contents %d 0 R >>" % (PAGE_W, PAGE_H, cid)))
            stream = "\n".join(ops).encode("latin-1", "replace")
            objs.append((cid, "<< /Length %d >>\nstream\n%s\nendstream"
                         % (len(stream), stream.decode("latin-1"))))

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = {}
        for num, body in sorted(objs):
            offsets[num] = len(out)
            out += ("%d 0 obj\n%s\nendobj\n" % (num, body)).encode("latin-1", "replace")
        xref_at = len(out)
        top = max(offsets) + 1
        out += ("xref\n0 %d\n" % top).encode()
        out += b"0000000000 65535 f \n"
        for i in range(1, top):
            out += ("%010d 00000 n \n" % offsets.get(i, 0)).encode()
        out += ("trailer\n<< /Size %d /Root 1 0 R /Info << /Title (%s) "
                "/Producer (Auditly) >> >>\nstartxref\n%d\n%%%%EOF\n"
                % (top, _esc(self.title), xref_at)).encode("latin-1", "replace")
        return bytes(out)
