#!/usr/bin/env python3
"""Generate UYAP-style PDF from UDF data.

Usage: python tools/udf_to_pdf.py uploads/ornek-1.udf outputs/ornek-1_generated.pdf
"""
import sys
import io
import re
import random
import string
from pathlib import Path

# Add project root to path
proj_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(proj_root))

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY

# ── Font registration & resolution ─────────────────────────────────────
# Default fallback fonts - use built-in fonts with Unicode support
# Note: Built-in fonts don't support Turkish chars, will be replaced by register_fonts()
FONT_REGULAR = 'Helvetica'  # Will be overridden by Arial or DejaVu if available
FONT_BOLD = 'Helvetica-Bold'
FONT_ITALIC = 'Helvetica-Oblique'
FONT_BOLD_ITALIC = 'Helvetica-BoldOblique'
FONT_MONO = 'Courier'

# Registry: family_name -> {regular, bold, italic, bold_italic}
_FONT_FAMILIES: dict[str, dict[str, str]] = {}
_REGISTERED_FONTS: set[str] = set()


def _register_one(name: str, path: str) -> bool:
    """Register a single TTF font if the file exists."""
    if name in _REGISTERED_FONTS:
        return True
    try:
        if Path(path).exists():
            pdfmetrics.registerFont(TTFont(name, path))
            _REGISTERED_FONTS.add(name)
            return True
    except Exception:
        pass
    return False


def register_fonts():
    """Register all font families that UDF files may reference.
    
    Supports both Windows and Linux paths for cross-platform compatibility.
    """
    global FONT_REGULAR, FONT_BOLD, FONT_ITALIC, FONT_BOLD_ITALIC, FONT_MONO

    # (family_key, reg, reg_name, bold, bold_name, ital, ital_name, bi, bi_name)
    families = [
        # Windows paths (for local testing)
        ('Times New Roman',
         'C:/Windows/Fonts/times.ttf',   'TimesNewRoman',
         'C:/Windows/Fonts/timesbd.ttf',  'TimesNewRomanBold',
         'C:/Windows/Fonts/timesi.ttf',   'TimesNewRomanItalic',
         'C:/Windows/Fonts/timesbi.ttf',  'TimesNewRomanBoldItalic'),
        ('Arial',
         'C:/Windows/Fonts/arial.ttf',   'Arial',
         'C:/Windows/Fonts/arialbd.ttf',  'ArialBold',
         'C:/Windows/Fonts/ariali.ttf',   'ArialItalic',
         'C:/Windows/Fonts/arialbi.ttf',  'ArialBoldItalic'),
        # Linux paths (Render/Docker)
        ('DejaVu Sans',
         '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',        'DejaVuSans',
         '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',    'DejaVuSansBold',
         '/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf', 'DejaVuSansOblique',
         '/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf', 'DejaVuSansBoldOblique'),
        ('Liberation Sans',
         '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf', 'LiberationSans',
         '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',    'LiberationSansBold',
         '/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf',  'LiberationSansItalic',
         '/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf', 'LiberationSansBoldItalic'),
    ]

    for entry in families:
        fam_key = entry[0]
        reg_path, reg_name = entry[1], entry[2]
        bold_path, bold_name = entry[3], entry[4]
        ital_path, ital_name = entry[5], entry[6]
        bi_path, bi_name = entry[7], entry[8]

        fam = {}
        if reg_path and _register_one(reg_name, reg_path):
            fam['regular'] = reg_name
        if bold_path and _register_one(bold_name, bold_path):
            fam['bold'] = bold_name
        if ital_path and _register_one(ital_name, ital_path):
            fam['italic'] = ital_name
        if bi_path and _register_one(bi_name, bi_path):
            fam['bold_italic'] = bi_name

        if fam:
            _FONT_FAMILIES[fam_key] = fam
    
    # Priority: Try DejaVu Sans → Liberation Sans → Times New Roman
    for font_family in ['DejaVu Sans', 'Liberation Sans', 'Arial', 'Times New Roman']:
        fam = _FONT_FAMILIES.get(font_family, {})
        if 'regular' in fam:
            FONT_REGULAR = fam['regular']
            FONT_BOLD = fam.get('bold', FONT_REGULAR)
            FONT_ITALIC = fam.get('italic', FONT_REGULAR)
            FONT_BOLD_ITALIC = fam.get('bold_italic', FONT_BOLD)
            break  # Use first available font family


register_fonts()


def _pick_font(bold=False, italic=False, family=None):
    """Return the correct font name for a bold/italic/family combination."""
    if family and family in _FONT_FAMILIES:
        fam = _FONT_FAMILIES[family]
        if bold and italic:
            return fam.get('bold_italic', fam.get('bold', fam.get('regular', FONT_BOLD_ITALIC)))
        if bold:
            return fam.get('bold', fam.get('regular', FONT_BOLD))
        if italic:
            return fam.get('italic', fam.get('regular', FONT_ITALIC))
        return fam.get('regular', FONT_REGULAR)
    # Fallback to global defaults (Times New Roman)
    if bold and italic:
        return FONT_BOLD_ITALIC
    if bold:
        return FONT_BOLD
    if italic:
        return FONT_ITALIC
    return FONT_REGULAR


def generate_uyap_code():
    """Generate a fake UYAP verification code."""
    parts = []
    for _ in range(4):
        part = ''.join(random.choices(string.ascii_letters + string.digits, k=7))
        parts.append(part)
    return ' - '.join(parts)


class UYAPPDFGenerator:
    """Generate UYAP-style Arabuluculuk Başvuru Formu PDF."""
    
    def __init__(self, width=A4[0], height=A4[1]):
        self.width = width
        self.height = height
        self.margin_left = 50
        self.margin_right = 50
        self.margin_top = 50
        self.margin_bottom = 80
        self.y = height - self.margin_top
        self.line_height = 14
        
    def create_pdf(self, data: dict, output_path: str = None) -> bytes:
        """Create PDF from extracted UDF data. Returns bytes if no output_path."""
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=A4)
        self.c = c
        self.y = self.height - self.margin_top
        
        # Draw content
        self._draw_header(data)
        self._draw_basvuru_info(data)
        self._draw_basvuru_sahibi(data)
        self._draw_karsi_taraflar(data)
        self._draw_basvuru_bilgileri(data)
        self._draw_not_bilgi(data)
        self._draw_declaration(data)
        self._draw_eki(data)
        self._draw_signatures(data)
        self._draw_not(data)
        self._draw_footer(data)
        
        c.save()
        pdf_bytes = buffer.getvalue()
        buffer.close()
        
        if output_path:
            with open(output_path, 'wb') as f:
                f.write(pdf_bytes)
        
        return pdf_bytes

    # ── birebir (raw text) PDF ──────────────────────────────────────────
    def create_pdf_birebir(self, data: dict, output_path: str = None,
                           udf_bytes: bytes = None) -> bytes:
        """Create PDF that reproduces the UDF content exactly (birebir).

        When *udf_bytes* is supplied the XML ``<elements>`` section is parsed
        so that each span's bold / italic / underline / font-size as well as
        paragraph alignment are faithfully reproduced.  When *udf_bytes* is
        ``None`` the method falls back to a heuristic text-only renderer.
        """
        import xml.etree.ElementTree as _ET

        raw_text = data.get('metadata', {}).get('text', '')
        if not raw_text:
            return self.create_pdf(data, output_path)

        # ── Try to parse paragraph elements from UDF XML ──
        paragraphs = None  # list[dict] with keys: alignment, spans
        if udf_bytes is not None:
            paragraphs = self._parse_udf_elements(udf_bytes, raw_text)

        if paragraphs is None:
            # Fallback: old heuristic birebir renderer (text-only)
            return self._create_pdf_birebir_text(data, output_path)

        # ── Apply page margins from XML if available ──
        page_fmt = None
        for p in paragraphs:
            if p.get('page_fmt'):
                page_fmt = p['page_fmt']
                break
        if page_fmt:
            self.margin_left = page_fmt['left_margin']
            self.margin_right = page_fmt['right_margin']
            self.margin_top = page_fmt['top_margin']
            self.margin_bottom = max(page_fmt['bottom_margin'], 80)  # min 80 for footer

        # ── Render with full formatting ──
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=A4)
        self.c = c
        self.y = self.height - self.margin_top

        # Separate footer paragraphs (after separator line or _type=='footer')
        footer_paras = []
        body_paras = []
        sep_found = False
        for para in paragraphs:
            if para.get('_type') == 'footer':
                footer_paras.append(para)
                continue
            if sep_found:
                footer_paras.append(para)
                continue
            text = ''.join(s['text'] for s in para.get('spans', [])).strip()
            if re.match(r'^[_\-=]{10,}$', text):
                sep_found = True
                continue
            body_paras.append(para)

        # Render body elements
        for para in body_paras:
            if para.get('_type') == 'table':
                self._render_table(para, footer_paras)
            else:
                self._render_paragraph(para, footer_paras)

        # Final footer
        self._draw_footer_formatted(footer_paras)

        c.save()
        pdf_bytes = buffer.getvalue()
        buffer.close()

        if output_path:
            with open(output_path, 'wb') as f:
                f.write(pdf_bytes)
        return pdf_bytes

    # ── Parse UDF XML <elements> into structured paragraph list ─────────
    @staticmethod
    def _parse_udf_elements(udf_bytes, cdata_text):
        """Parse ``<elements>`` from UDF content.xml.

        Returns a list of paragraph dicts.  Each dict has:

        - ``alignment``:  'left' | 'center' | 'justify'
        - ``spans``:      list of span dicts (text, bold, italic, underline,
                          size, family)
        - ``left_indent``, ``first_line_indent``, ``hanging``: float (pt)
        - ``space_above``, ``space_below``: float (pt)
        - ``line_spacing``: float (extra leading multiplier)
        - ``page_fmt``:   dict with page margins (shared across all paras)
        - ``_type``:      'paragraph' | 'table'
        """
        import xml.etree.ElementTree as ET
        import zipfile as _zf

        try:
            with _zf.ZipFile(io.BytesIO(udf_bytes), 'r') as z:
                xml_data = z.read('content.xml')
        except Exception:
            return None

        try:
            root = ET.fromstring(xml_data)
        except Exception:
            return None

        elements_el = root.find('elements')
        if elements_el is None:
            return None

        # ── Page margins from <properties> ──
        props_el = root.find('properties')
        page_fmt = None
        if props_el is not None:
            pf = props_el.find('pageFormat')
            if pf is not None:
                page_fmt = {
                    'left_margin': float(pf.attrib.get('leftMargin', '70.87')),
                    'right_margin': float(pf.attrib.get('rightMargin', '70.87')),
                    'top_margin': float(pf.attrib.get('topMargin', '28.35')),
                    'bottom_margin': float(pf.attrib.get('bottomMargin', '28.35')),
                }

        align_map = {'0': 'left', '1': 'center', '2': 'left', '3': 'justify'}

        # ── Helper: extract spans from any element that has content children ──
        def _extract_spans(el, inherit_bold=False, inherit_family=None,
                           inherit_size=12.0):
            """Walk an element's children and collect spans with offsets."""
            spans = []
            min_off = None
            max_off = 0
            for child in el:
                if child.tag in ('paragraph',):
                    # Nested paragraph inside table cell – recurse
                    continue
                if 'startOffset' not in child.attrib:
                    continue
                offset = int(child.attrib['startOffset'])
                length = int(child.attrib['length'])
                text = cdata_text[offset:offset + length]
                text = text.replace('\t', '        ')
                bold = child.attrib.get('bold',
                       el.attrib.get('bold', str(inherit_bold).lower())) == 'true'
                italic = child.attrib.get('italic', 'false') == 'true'
                underline = child.attrib.get('underline', 'false') == 'true'
                size = float(child.attrib.get('size',
                             el.attrib.get('size', str(inherit_size))))
                family = child.attrib.get('family',
                         el.attrib.get('family', inherit_family))
                spans.append({
                    'text': text,
                    'bold': bold,
                    'italic': italic,
                    'underline': underline,
                    'size': size,
                    'family': family,
                })
                if min_off is None:
                    min_off = offset
                max_off = max(max_off, offset + length)
            return spans, min_off, max_off

        def _para_dict(el, spans, _type='paragraph'):
            """Build a paragraph dict from an XML element + extracted spans."""
            alignment = align_map.get(el.attrib.get('Alignment', '0'), 'left')
            return {
                '_type': _type,
                'alignment': alignment,
                'spans': spans,
                'left_indent': float(el.attrib.get('LeftIndent', '0')),
                'right_indent': float(el.attrib.get('RightIndent', '0')),
                'first_line_indent': float(el.attrib.get('FirstLineIndent', '0')),
                'hanging': float(el.attrib.get('Hanging', '0')),
                'space_above': float(el.attrib.get('SpaceAbove', '0')),
                'space_below': float(el.attrib.get('SpaceBelow', '0')),
                'line_spacing': float(el.attrib.get('LineSpacing', '0')),
                'page_fmt': page_fmt,
            }

        result = []
        covered_end = 0

        # ── Walk top-level children of <elements> in document order ──
        for top_el in elements_el:
            if top_el.tag == 'paragraph':
                # --- Normal paragraph ---
                spans, para_start, para_end = _extract_spans(top_el)
                # Fill gap before this paragraph
                if para_start is not None and para_start > covered_end:
                    gap_text = cdata_text[covered_end:para_start]
                    if gap_text.strip():
                        for gap_line in gap_text.split('\n'):
                            gl = gap_line.rstrip('\r')
                            result.append({
                                '_type': 'paragraph',
                                'alignment': 'left',
                                'spans': [{'text': gl, 'bold': False,
                                           'italic': False, 'underline': False,
                                           'size': 12, 'family': None}],
                                'left_indent': 0, 'right_indent': 0,
                                'first_line_indent': 0, 'hanging': 0,
                                'space_above': 0, 'space_below': 0,
                                'line_spacing': 0, 'page_fmt': page_fmt,
                            })
                if para_start is not None:
                    covered_end = max(covered_end, para_end)
                result.append(_para_dict(top_el, spans))

            elif top_el.tag == 'table':
                # --- Table: rows × cells × paragraphs ---
                table_rows = []  # list of rows; row = list of cell_paras
                tbl_min_off = None
                tbl_max_off = 0
                for row_el in top_el.findall('row'):
                    row_cells = []
                    for cell_el in row_el.findall('cell'):
                        cell_paras = []
                        for cp_el in cell_el.findall('paragraph'):
                            spans, s, e = _extract_spans(
                                cp_el,
                                inherit_bold=cp_el.attrib.get('bold', 'false') == 'true',
                                inherit_family=cp_el.attrib.get('family'),
                                inherit_size=float(cp_el.attrib.get('size', '12')),
                            )
                            cell_paras.append(_para_dict(cp_el, spans, 'table_cell'))
                            if s is not None:
                                if tbl_min_off is None:
                                    tbl_min_off = s
                                tbl_min_off = min(tbl_min_off, s)
                                tbl_max_off = max(tbl_max_off, e)
                        row_cells.append(cell_paras)
                    table_rows.append(row_cells)

                # Fill gap before table
                if tbl_min_off is not None and tbl_min_off > covered_end:
                    gap_text = cdata_text[covered_end:tbl_min_off]
                    if gap_text.strip():
                        for gap_line in gap_text.split('\n'):
                            gl = gap_line.rstrip('\r')
                            result.append({
                                '_type': 'paragraph',
                                'alignment': 'left',
                                'spans': [{'text': gl, 'bold': False,
                                           'italic': False, 'underline': False,
                                           'size': 12, 'family': None}],
                                'left_indent': 0, 'right_indent': 0,
                                'first_line_indent': 0, 'hanging': 0,
                                'space_above': 0, 'space_below': 0,
                                'line_spacing': 0, 'page_fmt': page_fmt,
                            })
                if tbl_min_off is not None:
                    covered_end = max(covered_end, tbl_max_off)

                result.append({
                    '_type': 'table',
                    'rows': table_rows,
                    'border': top_el.attrib.get('border', 'borderNone'),
                    'page_fmt': page_fmt,
                })

            elif top_el.tag == 'footer':
                # --- Footer element: parse its paragraphs ---
                for fp_el in top_el.findall('paragraph'):
                    spans, s, e = _extract_spans(
                        fp_el,
                        inherit_family=fp_el.attrib.get('family'),
                        inherit_size=float(fp_el.attrib.get('size', '12')),
                    )
                    if s is not None:
                        covered_end = max(covered_end, e)
                    result.append(_para_dict(fp_el, spans, 'footer'))

        # ── Fill trailing gap ──
        if covered_end < len(cdata_text):
            trailing = cdata_text[covered_end:]
            if trailing.strip():
                for tline in trailing.split('\n'):
                    tline = tline.rstrip('\r')
                    result.append({
                        '_type': 'paragraph',
                        'alignment': 'left',
                        'spans': [{'text': tline, 'bold': False,
                                   'italic': False, 'underline': False,
                                   'size': 9, 'family': None}],
                        'left_indent': 0, 'right_indent': 0,
                        'first_line_indent': 0, 'hanging': 0,
                        'space_above': 0, 'space_below': 0,
                        'line_spacing': 0, 'page_fmt': page_fmt,
                    })

        return result if result else None

    # ── Render one paragraph with per-span formatting ───────────────────
    def _render_paragraph(self, para, footer_paras):
        """Render a single paragraph dict to the canvas."""
        c = self.c
        alignment = para['alignment']
        spans = para.get('spans', [])

        if not spans:
            self.y -= self.line_height * 0.6
            return

        # Combine all span texts
        full_text = ''.join(s['text'] for s in spans)
        display_text = full_text.rstrip('\n').rstrip('\r')

        # SpaceAbove
        sa = para.get('space_above', 0)
        if sa > 0:
            self.y -= sa

        # Empty paragraph → vertical space
        if not display_text.strip():
            self.y -= self.line_height * 0.6
            sb = para.get('space_below', 0)
            if sb > 0:
                self.y -= sb
            return

        # Page break check
        if self.y < self.margin_bottom + 20:
            self._draw_footer_formatted(footer_paras)
            c.showPage()
            self.y = self.height - self.margin_top

        # Determine the dominant size for line height
        max_size = max(s['size'] for s in spans if s['text'].strip()) if any(s['text'].strip() for s in spans) else 12
        line_spacing = para.get('line_spacing', 0)
        line_h = max_size + 3 + (line_spacing * max_size if line_spacing > 0 else 0)

        # Paragraph-level indents (in points — UDF uses Java points ≈ PDF points)
        left_indent = para.get('left_indent', 0)
        first_line_indent = para.get('first_line_indent', 0)

        if alignment == 'center':
            self._draw_centered_spans(spans, display_text, line_h)
        else:
            self._draw_left_spans(spans, display_text, line_h, footer_paras,
                                  left_indent=left_indent,
                                  first_line_indent=first_line_indent)

        # SpaceBelow
        sb = para.get('space_below', 0)
        if sb > 0:
            self.y -= sb

    # ── Render a table element ──────────────────────────────────────────
    def _render_table(self, table_el, footer_paras):
        """Render a UDF table (rows × cells) side by side."""
        rows = table_el.get('rows', [])
        if not rows:
            return

        c = self.c
        usable_width = self.width - self.margin_left - self.margin_right

        for row_cells in rows:
            n_cells = len(row_cells)
            if n_cells == 0:
                continue
            col_width = usable_width / n_cells

            # Collect text for each cell to compute row height
            cell_contents = []  # list of list of (spans, alignment)
            max_lines = 0
            for cell_paras in row_cells:
                cell_spans_list = []
                for cp in cell_paras:
                    spans = cp.get('spans', [])
                    text = ''.join(s['text'] for s in spans).rstrip('\n\r')
                    if text.strip():
                        cell_spans_list.append((spans, cp.get('alignment', 'left')))
                if not cell_spans_list:
                    cell_spans_list.append(([{'text': '', 'bold': False,
                                              'italic': False, 'underline': False,
                                              'size': 11, 'family': None}], 'left'))
                cell_contents.append(cell_spans_list)
                max_lines = max(max_lines, len(cell_spans_list))

            row_height = max_lines * (14 + 3)

            # Page break
            if self.y - row_height < self.margin_bottom + 20:
                self._draw_footer_formatted(footer_paras)
                c.showPage()
                self.y = self.height - self.margin_top

            # Draw each cell
            for ci, cell_lines in enumerate(cell_contents):
                cell_x = self.margin_left + ci * col_width
                cell_y = self.y
                for spans, alignment in cell_lines:
                    text = ''.join(s['text'] for s in spans).rstrip('\n\r')
                    if not text.strip():
                        cell_y -= 14
                        continue
                    # Use first span's style for simplicity
                    s0 = spans[0]
                    font = _pick_font(s0['bold'], s0['italic'], s0.get('family'))
                    sz = s0['size']
                    c.setFont(font, sz)
                    c.drawString(cell_x, cell_y, text)
                    if s0.get('underline'):
                        w = c.stringWidth(text, font, sz)
                        c.line(cell_x, cell_y - 1.5, cell_x + w, cell_y - 1.5)
                    cell_y -= sz + 3

            self.y -= row_height

    def _draw_centered_spans(self, spans, display_text, line_h):
        """Draw centered text with per-span formatting."""
        c = self.c
        text = display_text.strip()
        if not text:
            self.y -= line_h
            return

        # Calculate total width
        total_w = 0
        for s in spans:
            t = s['text'].rstrip('\n').rstrip('\r')
            if not t:
                continue
            font = _pick_font(s['bold'], s['italic'], s.get('family'))
            total_w += c.stringWidth(t, font, s['size'])

        start_x = (self.width - total_w) / 2
        x = start_x

        for s in spans:
            t = s['text'].rstrip('\n').rstrip('\r')
            if not t:
                continue
            font = _pick_font(s['bold'], s['italic'], s.get('family'))
            c.setFont(font, s['size'])
            c.drawString(x, self.y, t)
            w = c.stringWidth(t, font, s['size'])
            if s['underline']:
                c.line(x, self.y - 1.5, x + w, self.y - 1.5)
            x += w

        self.y -= line_h

    def _draw_left_spans(self, spans, display_text, line_h, footer_paras,
                         left_indent=0, first_line_indent=0):
        """Draw left/justify text with per-span formatting and word wrapping."""
        c = self.c
        effective_left = self.margin_left + left_indent
        usable_width = self.width - effective_left - self.margin_right

        # Build a flat list of "styled words"
        styled_words = []
        for s in spans:
            t = s['text'].rstrip('\n').rstrip('\r')
            if not t:
                continue
            style = {
                'bold': s['bold'],
                'italic': s['italic'],
                'underline': s['underline'],
                'size': s['size'],
                'family': s.get('family'),
            }
            # Leading whitespace → indent
            if not styled_words and t != t.lstrip(' \t'):
                stripped = t.lstrip(' \t')
                leading = t[:len(t) - len(stripped)]
                if leading:
                    styled_words.append({'text': leading, **style, '_indent': True})
                t = stripped

            parts = re.split(r'(\s+)', t)
            for p in parts:
                if p:
                    styled_words.append({'text': p, **style, '_indent': False})

        if not styled_words:
            self.y -= line_h
            return

        # Word-wrap styled words into lines
        lines = []
        current_line = []
        current_width = 0
        indent_width = 0

        for sw in styled_words:
            font = _pick_font(sw['bold'], sw['italic'], sw.get('family'))
            w = c.stringWidth(sw['text'], font, sw['size'])

            if sw.get('_indent'):
                indent_width = w
                current_line.append(sw)
                current_width += w
                continue

            if current_width + w > usable_width and current_line:
                lines.append(current_line)
                current_line = []
                current_width = indent_width

            current_line.append(sw)
            current_width += w

        if current_line:
            lines.append(current_line)

        # Draw each wrapped line
        for li, line_words in enumerate(lines):
            if self.y < self.margin_bottom + 20:
                self._draw_footer_formatted(footer_paras)
                c.showPage()
                self.y = self.height - self.margin_top

            x = effective_left
            # Apply first-line indent only on first line
            if li == 0 and first_line_indent > 0:
                x += first_line_indent

            for sw in line_words:
                font = _pick_font(sw['bold'], sw['italic'], sw.get('family'))
                c.setFont(font, sw['size'])
                c.drawString(x, self.y, sw['text'])
                w = c.stringWidth(sw['text'], font, sw['size'])
                if sw['underline']:
                    c.line(x, self.y - 1.5, x + w, self.y - 1.5)
                x += w

            self.y -= line_h

    def _draw_footer_formatted(self, footer_paras):
        """Draw footer with formatting info from parsed paragraphs."""
        footer_y = 70
        c = self.c

        # Separator line
        c.setStrokeColorRGB(0, 0, 0)
        c.line(self.margin_left, footer_y + 30,
               self.width - self.margin_right, footer_y + 30)

        # Footer text
        fy = footer_y + 18
        for para in footer_paras:
            text = ''.join(s['text'] for s in para['spans']).strip()
            if not text:
                continue
            # Use smaller font for footer
            sz = 9
            font = FONT_REGULAR
            for s in para['spans']:
                if s['bold']:
                    font = FONT_BOLD
                    break
            c.setFont(font, sz)
            if para['alignment'] == 'center':
                c.drawCentredString(self.width / 2, fy, text)
            else:
                c.drawString(self.margin_left, fy, text)
            fy -= 12

        # UYAP verification code
        c.setFont(FONT_REGULAR, 7)
        prefix = 'UYAP Bilişim Sistemindeki bu dokümana http://vatandas.uyap.gov.tr adresinden '
        uyap_code = generate_uyap_code()
        suffix = ' ile erişebilirsiniz.'
        prefix_width = c.stringWidth(prefix, FONT_REGULAR, 7)
        code_width = c.stringWidth(uyap_code, FONT_MONO, 10)
        suffix_width = c.stringWidth(suffix, FONT_REGULAR, 7)
        total_width = prefix_width + code_width + suffix_width
        start_x = (self.width - total_width) / 2
        c.drawString(start_x, fy, prefix)
        c.setFont(FONT_MONO, 10)
        c.drawString(start_x + prefix_width, fy, uyap_code)
        c.setFont(FONT_REGULAR, 7)
        c.drawString(start_x + prefix_width + code_width, fy, suffix)

    # ── Fallback text-only birebir renderer ──────────────────────────────
    def _create_pdf_birebir_text(self, data, output_path=None):
        """Fallback birebir renderer using raw text only (no XML formatting)."""
        raw_text = data.get('metadata', {}).get('text', '')
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=A4)
        self.c = c
        self.y = self.height - self.margin_top

        all_lines = raw_text.split('\n')
        header_end = 0
        footer_start = len(all_lines)

        for i, ln in enumerate(all_lines):
            if re.match(r'ARABULUCULUK\s+BA[ŞS]VURU\s+FORMU', ln.strip(), re.IGNORECASE):
                header_end = i + 1
                break

        for i in range(len(all_lines) - 1, -1, -1):
            if re.match(r'^[_\-=]{10,}$', all_lines[i].strip()):
                footer_start = i
                break

        font_size = 11
        center_x = self.width / 2
        usable_width = self.width - self.margin_left - self.margin_right

        for i in range(header_end):
            stripped = all_lines[i].strip()
            if not stripped:
                self.y -= self.line_height
                continue
            c.setFont(FONT_BOLD, 12)
            c.drawCentredString(center_x, self.y, stripped)
            self.y -= self.line_height
        self.y -= self.line_height / 2

        for i in range(header_end, footer_start):
            raw_line = all_lines[i]
            stripped = raw_line.strip()
            if self.y < self.margin_bottom + 20:
                self._draw_footer_birebir(all_lines, footer_start)
                c.showPage()
                self.y = self.height - self.margin_top
            if not stripped:
                self.y -= self.line_height * 0.6
                continue
            expanded = self._expand_tabs(raw_line)
            leading = len(expanded) - len(expanded.lstrip(' '))
            c.setFont(FONT_REGULAR, font_size)
            indent_px = c.stringWidth(' ' * leading, FONT_REGULAR, font_size)
            x = self.margin_left + indent_px
            max_w = usable_width - indent_px
            if max_w < 50:
                max_w = usable_width
                x = self.margin_left
            wrapped = self._wrap_text(expanded.strip(), max_w, size=font_size)
            for wline in wrapped:
                if self.y < self.margin_bottom + 20:
                    self._draw_footer_birebir(all_lines, footer_start)
                    c.showPage()
                    self.y = self.height - self.margin_top
                c.setFont(FONT_REGULAR, font_size)
                c.drawString(x, self.y, wline)
                self.y -= self.line_height

        self._draw_footer_birebir(all_lines, footer_start)
        c.save()
        pdf_bytes = buffer.getvalue()
        buffer.close()
        if output_path:
            with open(output_path, 'wb') as f:
                f.write(pdf_bytes)
        return pdf_bytes

    def _draw_footer_birebir(self, all_lines, footer_start):
        """Draw the footer section for birebir mode."""
        footer_y = 70

        # Separator line
        self.c.setStrokeColorRGB(0, 0, 0)
        self.c.line(self.margin_left, footer_y + 30,
                    self.width - self.margin_right, footer_y + 30)

        # Footer text lines (after separator)
        self.c.setFont(FONT_REGULAR, 9)
        fy = footer_y + 18
        for i in range(footer_start + 1, len(all_lines)):
            ln = all_lines[i].strip()
            if not ln:
                continue
            self.c.drawString(self.margin_left, fy, ln)
            fy -= 12

        # UYAP verification code
        self.c.setFont(FONT_REGULAR, 7)
        prefix = 'UYAP Bilişim Sistemindeki bu dokümana http://vatandas.uyap.gov.tr adresinden '
        uyap_code = generate_uyap_code()
        suffix = ' ile erişebilirsiniz.'
        full_text = prefix + uyap_code + suffix
        prefix_width = self.c.stringWidth(prefix, FONT_REGULAR, 7)
        code_width = self.c.stringWidth(uyap_code, FONT_MONO, 10)
        suffix_width = self.c.stringWidth(suffix, FONT_REGULAR, 7)
        total_width = prefix_width + code_width + suffix_width
        start_x = (self.width - total_width) / 2
        self.c.drawString(start_x, fy, prefix)
        self.c.setFont(FONT_MONO, 10)
        self.c.drawString(start_x + prefix_width, fy, uyap_code)
        self.c.setFont(FONT_REGULAR, 7)
        self.c.drawString(start_x + prefix_width + code_width, fy, suffix)

    @staticmethod
    def _expand_tabs(line, tab_size=8):
        """Expand tab characters to spaces using fixed tab stops."""
        result = []
        col = 0
        for ch in line:
            if ch == '\t':
                spaces = tab_size - (col % tab_size)
                result.append(' ' * spaces)
                col += spaces
            else:
                result.append(ch)
                col += 1
        return ''.join(result)
    
    def _check_page_break(self, needed_height=50):
        """Check if we need a new page."""
        if self.y < self.margin_bottom + needed_height:
            self.c.showPage()
            self.y = self.height - self.margin_top
    
    def _draw_text(self, text, x=None, font=None, size=12, bold=False):
        """Draw text at current y position."""
        if x is None:
            x = self.margin_left
        if font is None:
            font = FONT_BOLD if bold else FONT_REGULAR
        
        self.c.setFont(font, size)
        self.c.drawString(x, self.y, text)
    
    def _draw_line(self, text, x=None, font=None, size=12, bold=False, line_height=None):
        """Draw text and move to next line."""
        self._draw_text(text, x, font, size, bold)
        self.y -= (line_height or self.line_height)
    
    def _draw_label_value(self, label, value, label_width=180):
        """Draw label: value pair with proper wrapping."""
        self._draw_text(label, self.margin_left)
        
        value_x = self.margin_left + label_width
        max_value_width = self.width - value_x - self.margin_right
        
        # Wrap value text based on pixel width
        value_lines = self._wrap_text(f': {value}', max_value_width)
        
        for i, line in enumerate(value_lines):
            if i > 0:
                self._check_page_break()
            self._draw_text(line if i == 0 else f'  {line}', value_x)
            self.y -= self.line_height
    
    def _draw_label_value_nospace(self, label, value, label_width=180):
        """Draw label:value pair without space after colon (UYAP Vekili format)."""
        self._draw_text(label, self.margin_left)
        
        value_x = self.margin_left + label_width
        max_value_width = self.width - value_x - self.margin_right
        
        # Note: No space after colon
        value_lines = self._wrap_text(f':{value}', max_value_width)
        
        for i, line in enumerate(value_lines):
            if i > 0:
                self._check_page_break()
            self._draw_text(line if i == 0 else f'  {line}', value_x)
            self.y -= self.line_height
    
    def _wrap_text(self, text, max_width, size=12):
        """Wrap text to fit within max_width, returns list of lines."""
        self.c.setFont(FONT_REGULAR, size)
        
        if self.c.stringWidth(text, FONT_REGULAR, size) <= max_width:
            return [text]
        
        words = text.split()
        lines = []
        current_line = []
        
        for word in words:
            test_line = ' '.join(current_line + [word])
            if self.c.stringWidth(test_line, FONT_REGULAR, size) <= max_width:
                current_line.append(word)
            else:
                if current_line:
                    lines.append(' '.join(current_line))
                current_line = [word]
        if current_line:
            lines.append(' '.join(current_line))
        
        return lines if lines else [text]
    
    def _draw_wrapped_text(self, text, max_width=None, size=12, justify=False):
        """Draw text with word wrapping."""
        if max_width is None:
            max_width = self.width - self.margin_left - self.margin_right
        
        lines = self._wrap_text(text, max_width, size)
        
        for line in lines:
            self._check_page_break()
            self._draw_line(line, size=size)
    
    def _draw_header(self, data):
        """Draw T.C. header and title."""
        # Center aligned header
        center_x = self.width / 2
        
        self.c.setFont(FONT_BOLD, 12)
        self.c.drawCentredString(center_x, self.y, 'T.C.')
        self.y -= self.line_height
        
        buro = data.get('arabuluculuk_burosu', 'İzmir Arabuluculuk Bürosu')
        self.c.drawCentredString(center_x, self.y, buro)
        self.y -= self.line_height
        
        self.c.drawCentredString(center_x, self.y, 'ARABULUCULUK BAŞVURU FORMU')
        self.y -= self.line_height * 2
    
    def _draw_basvuru_info(self, data):
        """Draw başvuru numarası and tarihi."""
        fields = data.get('fields', data)
        
        self._draw_label_value('BAŞVURU NUMARASI', fields.get('basvuru_numarasi', ''))
        self._draw_label_value('BAŞVURU TARİHİ', fields.get('basvuru_tarihi', ''))
        self.y -= self.line_height / 2
    
    def _draw_basvuru_sahibi(self, data):
        """Draw başvuru sahibi bilgileri section(s) - supports multiple applicants."""
        fields = data.get('fields', data)
        
        # Get list of applicants (new format) or create single-item list from old format
        basvuru_sahipleri = fields.get('basvuru_sahipleri', [])
        
        # Backwards compatibility: if no basvuru_sahipleri array, use old single fields
        if not basvuru_sahipleri:
            basvuru_sahipleri = [{
                'tc_kimlik_no': fields.get('tc_kimlik_no', ''),
                'vergi_no': fields.get('vergi_no', ''),
                'adi_soyadi': fields.get('adi_soyadi', ''),
                'adres': fields.get('adres', ''),
                'vekil': fields.get('vekil', ''),
                'kisi_turu': fields.get('kisi_turu', 'gercek'),
                'telefon': fields.get('basvuru_telefonu', {})
            }]
        
        for idx, applicant in enumerate(basvuru_sahipleri):
            self._check_page_break(80)
            self._draw_line('BAŞVURU SAHİBİ BİLGİLERİ', bold=True)
            
            # TC/Vergi No based on entity type
            kisi_turu = applicant.get('kisi_turu', 'gercek')
            tc = applicant.get('tc_kimlik_no', '')
            vergi_no = applicant.get('vergi_no', '')
            
            if kisi_turu == 'tuzel' and vergi_no:
                self._draw_label_value('Vergi No', vergi_no)
            elif tc:
                self._draw_label_value('TC Kimlik No', tc)
            
            self._draw_label_value('Adı Soyadı', applicant.get('adi_soyadi', ''))
            
            # Address with phone
            adres = applicant.get('adres', '')
            tel = applicant.get('telefon', {})
            tel_type = 'cep_tel'
            if isinstance(tel, dict):
                # Use compact (no spaces) to match original UYAP format
                tel_str = tel.get('compact', tel.get('pretty', ''))
                tel_type = tel.get('type', 'cep_tel')
            else:
                tel_str = str(tel) if tel else ''
            
            # Choose correct label based on phone type
            tel_label = 'Cep Tel' if tel_type == 'cep_tel' else 'Tel'
            
            if tel_str:
                adres_full = f'{adres}  {tel_label} : {tel_str}'
            else:
                adres_full = adres
            
            self._draw_label_value('Adres', adres_full)
            
            # Vekil - check applicant's vekil first, then global vekil for first applicant
            vekil = applicant.get('vekil', '')
            if not vekil and idx == 0:
                vekil = fields.get('vekil', '')
            
            vekil_tel = fields.get('vekil_telefonu', {})
            if isinstance(vekil_tel, dict):
                # Use compact format
                vekil_tel_str = vekil_tel.get('compact', vekil_tel.get('pretty', ''))
            else:
                vekil_tel_str = str(vekil_tel) if vekil_tel else ''
            
            if vekil:
                if vekil_tel_str and 'TEL' not in vekil.upper():
                    vekil_display = f'{vekil} (TEL: {vekil_tel_str})'
                else:
                    vekil_display = vekil
                # Original UYAP format: ":Av." (no space after colon)
                self._draw_label_value_nospace('Vekili', vekil_display)
            
            self.y -= self.line_height / 2
    
    def _draw_karsi_taraflar(self, data):
        """Draw diğer taraf bilgileri sections."""
        fields = data.get('fields', data)
        karsi_taraflar = fields.get('karsi_taraflar', [])
        
        for kt in karsi_taraflar:
            # Skip if looks like footer data
            if 'Bilgi Sahibi' in kt.get('adi_soyadi', ''):
                continue
            
            self._check_page_break(80)
            self._draw_line('DİĞER TARAF BİLGİLERİ', bold=True)
            
            # TC/Vergi No based on entity type
            kisi_turu = kt.get('kisi_turu', 'unknown')
            tc = kt.get('tc_kimlik_no', '')
            vergi_no = kt.get('vergi_no', '')
            
            if kisi_turu == 'tuzel' and vergi_no:
                self._draw_label_value('Vergi No', vergi_no)
            elif tc:
                self._draw_label_value('TC Kimlik No', tc)
            
            name = kt.get('adi_soyadi', '')
            self._draw_label_value('Adı Soyadı', name)
            
            adres = kt.get('adres', '')
            tel = kt.get('telefon', {})
            tel_type = 'cep_tel'
            if isinstance(tel, dict):
                # Use compact format (no spaces) to match original UYAP
                tel_str = tel.get('compact', tel.get('pretty', ''))
                tel_type = tel.get('type', 'cep_tel')
            else:
                tel_str = str(tel) if tel else ''
            
            # Original UYAP format: phone number directly after address, no label
            # e.g., "BELEDİYE İŞ MERKEZİ K:2 NO:94 MERKEZ AĞRI 04722165323"
            if tel_str and tel_str not in adres:
                adres_full = f'{adres} {tel_str}' if adres else ''
            else:
                adres_full = adres
            
            if adres_full:
                self._draw_label_value('Adres', adres_full)
            
            self.y -= self.line_height / 2
    
    def _draw_basvuru_bilgileri(self, data):
        """Draw başvuru bilgileri section."""
        fields = data.get('fields', data)
        
        self._check_page_break(60)
        self._draw_line('BAŞVURU BİLGİLERİ', bold=True)

        dava_turu = fields.get('dava_turu', '')
        if dava_turu:
            self._draw_label_value('Dava Türü', dava_turu)

        # Uyuşmazlık Türü (can be very long — render fully)
        uyusmazlik = fields.get('uyusmazlik_turu', '')
        if uyusmazlik:
            self._draw_label_value('Uyuşmazlık Türü', uyusmazlik)

        bilgi_sahibi = fields.get('bilgi_sahibi_mi', '')
        if bilgi_sahibi:
            self._draw_label_value('Diğer Taraf Bilgi Sahibi Mi', bilgi_sahibi)
        
        # Use extracted value
        muracaat = fields.get('basvuru_konusu_muracaat_durumu', '')
        if muracaat:
            self._draw_label_value('Başvuru Konusu Müracaat Durumu', muracaat)
        
        self.y -= self.line_height / 2

    def _draw_not_bilgi(self, data):
        """Draw the informational NOT section (about tarafların tacir olup olmadıkları...)."""
        fields = data.get('fields', data)
        not_bilgi = fields.get('not_bilgi_metni', '')
        if not not_bilgi:
            return  # Skip if not present in UDF

        self._check_page_break(60)
        label = 'NOT: '
        self.c.setFont(FONT_BOLD, 10)
        label_width = self.c.stringWidth(label, FONT_BOLD, 10)
        value_x = self.margin_left + label_width
        max_value_width = self.width - value_x - self.margin_right
        not_lines = self._wrap_text(not_bilgi, max_value_width, size=10)

        # Draw label on first line
        self._draw_text(label, self.margin_left, font=FONT_BOLD, size=10)
        self.c.setFont(FONT_REGULAR, 10)
        for i, line in enumerate(not_lines):
            x = value_x if i == 0 else self.margin_left
            self._draw_text(line, x, font=FONT_REGULAR, size=10)
            self.y -= self.line_height
        self.y -= self.line_height / 2

    def _draw_declaration(self, data):
        """Draw the declaration text — uses extracted text from UDF."""
        fields = data.get('fields', data)
        beyan = fields.get('beyan_metni', '')
        if not beyan:
            # Fallback to standard text
            beyan = (
                "Başkaca bir usul kararlaştırılmadıkça arabulucunun taraflarca seçileceğini bildiğimi, "
                "başvuru dilekçesinde yer alan tüm açıklamaları okuyup anladığımı, başvuru konusuna ilişkin "
                "sahip olduğum tüm bilgi ve belgeleri işbu başvuru dilekçesi ve ekinde doğru ve eksiksiz "
                "olarak ibraz ettiğimi beyan eder, işbu başvurunun işleme konulmasını arz ve talep ederim."
            )
        self._check_page_break(80)
        self._draw_wrapped_text(beyan)
        self.y -= self.line_height / 2
    
    def _draw_eki(self, data):
        """Draw eki section — uses extracted text from UDF."""
        fields = data.get('fields', data)
        eki = fields.get('eki_metni', '') or 'VEKALETNAME SURETİ, ARABULUCULUK BAŞVURU ÖN FORMU'

        self._check_page_break(30)
        
        self.c.setFont(FONT_BOLD, 12)
        label = 'EKİ : '
        label_w = self.c.stringWidth(label, FONT_BOLD, 12)
        self._draw_text(label, self.margin_left)
        self.c.setFont(FONT_REGULAR, 12)
        max_w = self.width - self.margin_left - label_w - self.margin_right
        eki_lines = self._wrap_text(eki, max_w, size=12)
        for i, line in enumerate(eki_lines):
            x = self.margin_left + label_w if i == 0 else self.margin_left + label_w
            self._draw_text(line, x)
            self.y -= self.line_height
        self.y -= self.line_height
    
    def _draw_signatures(self, data):
        """Draw signature blocks - multiple applicants shown side by side."""
        self._check_page_break(80)
        
        fields = data.get('fields', data)
        
        # Get all signature names
        imza_isimleri = fields.get('imza_isimleri', [])
        if not imza_isimleri:
            # Fallback to single name
            name = fields.get('imza_adi', '') or fields.get('adi_soyadi', '')
            if name:
                imza_isimleri = [name]
        
        num_applicants = len(imza_isimleri)
        
        if num_applicants <= 1:
            # Single applicant - original layout
            self.c.setFont(FONT_BOLD, 11)
            self._draw_text('Başvurucu', self.margin_left)
            self.y -= self.line_height
            
            self.c.setFont(FONT_REGULAR, 11)
            if imza_isimleri:
                name = imza_isimleri[0]
                name_parts = name.split()
                title_name = ' '.join(p.capitalize() for p in name_parts)
                self._draw_text(title_name, self.margin_left)
            self.y -= self.line_height
        else:
            # Multiple applicants - side by side layout
            available_width = self.width - self.margin_left - self.margin_right
            col_width = available_width / num_applicants
            
            # Draw "Başvurucu" labels
            self.c.setFont(FONT_BOLD, 11)
            for i in range(num_applicants):
                x_pos = self.margin_left + (i * col_width)
                self._draw_text('Başvurucu', x_pos)
            self.y -= self.line_height
            
            # Draw names
            self.c.setFont(FONT_REGULAR, 11)
            for i, name in enumerate(imza_isimleri):
                x_pos = self.margin_left + (i * col_width)
                name_parts = name.split()
                title_name = ' '.join(p.capitalize() for p in name_parts)
                self._draw_text(title_name, x_pos)
            self.y -= self.line_height
        
        # Vekil - use imza_vekili if available, otherwise vekil
        vekil = fields.get('imza_vekili', '') or fields.get('vekil', '')
        if vekil:
            # Clean vekil name - remove phone info
            vekil_clean = re.sub(r'\s*\(TEL[^)]*\)?', '', vekil, flags=re.IGNORECASE).strip()
            vekil_clean = re.sub(r'\s*TEL[:\s]*\d+', '', vekil_clean, flags=re.IGNORECASE).strip()
            # Format: "Vekili Av. NAME" or just "VekiliAv. NAME" based on original
            if vekil_clean.startswith('Av.'):
                self._draw_text(f'Vekili {vekil_clean}', self.margin_left)
            else:
                self._draw_text(f'Vekili Av. {vekil_clean}', self.margin_left)
            self.y -= self.line_height
        
        self.y -= self.line_height
    
    def _draw_not(self, data):
        """Draw NOT section — uses extracted text from UDF."""
        fields = data.get('fields', data)
        not_text = fields.get('not_atama_metni', '') or 'BAŞVURUCU/VEKİLİNİN TALEBİ İLE İZMİR KOMİSYONUNDAN ATAMA YAPILMIŞTIR.'
        
        self._check_page_break(30)
        
        label = 'NOT : '
        
        self.c.setFont(FONT_BOLD, 10)
        label_width = self.c.stringWidth(label, FONT_BOLD, 10)
        
        # Wrap NOT text using available width after label
        value_x = self.margin_left + label_width
        max_value_width = self.width - value_x - self.margin_right
        not_lines = self._wrap_text(not_text, max_value_width, size=10)
        
        # Draw label
        self._draw_text(label, self.margin_left, font=FONT_BOLD, size=10)
        
        # Draw wrapped text
        self.c.setFont(FONT_REGULAR, 10)
        for i, line in enumerate(not_lines):
            self._draw_text(line, value_x, font=FONT_REGULAR, size=10)
            self.y -= self.line_height
        
        self.y -= self.line_height
    
    def _draw_footer(self, data):
        """Draw footer with address and UYAP code."""
        # Draw separator line
        footer_y = 70
        
        self.c.setStrokeColorRGB(0, 0, 0)
        self.c.line(self.margin_left, footer_y + 30, self.width - self.margin_right, footer_y + 30)
        
        # Footer text
        self.c.setFont(FONT_REGULAR, 9)
        
        # Get footer adres from fields (extracted from UDF)
        fields = data.get('fields', {})
        buro = fields.get('footer_adres') or data.get('arabuluculuk_burosu') or 'İzmir Arabuluculuk Bürosu'
        
        # Get clerk name from "ayrintili_bilgi_icin" field
        katip = fields.get('ayrintili_bilgi_icin') or 'ZABIT KÂTİBİ'
        
        self.c.drawString(self.margin_left, footer_y + 18, f'Adres : {buro}')
        self.c.drawString(self.margin_left + 200, footer_y + 18, f'Ayrıntılı Bilgi İçin : {katip}')
        
        # UYAP code - centered on line
        self.c.setFont(FONT_REGULAR, 7)
        prefix = 'UYAP Bilişim Sistemindeki bu dokümana http://vatandas.uyap.gov.tr adresinden '
        uyap_code = generate_uyap_code()
        suffix = ' ile erişebilirsiniz.'
        
        # Calculate total width and center
        full_text = prefix + uyap_code + suffix
        prefix_width = self.c.stringWidth(prefix, FONT_REGULAR, 7)
        code_width = self.c.stringWidth(uyap_code, FONT_MONO, 10)
        suffix_width = self.c.stringWidth(suffix, FONT_REGULAR, 7)
        total_width = prefix_width + code_width + suffix_width
        
        # Start position to center
        start_x = (self.width - total_width) / 2
        
        self.c.drawString(start_x, footer_y + 5, prefix)
        self.c.setFont(FONT_MONO, 10)
        self.c.drawString(start_x + prefix_width, footer_y + 5, uyap_code)
        self.c.setFont(FONT_REGULAR, 7)
        self.c.drawString(start_x + prefix_width + code_width, footer_y + 5, suffix)


def generate_pdf_from_udf(udf_path: str, output_path: str = None,
                          mode: str = 'birebir') -> bytes:
    """Generate PDF from a UDF file.

    Args:
        mode: 'birebir' (raw text, default) or 'structured' (field-based).
    """
    from mini_udf_service import parse_udf_bytes
    
    with open(udf_path, 'rb') as f:
        udf_bytes = f.read()
    
    parsed = parse_udf_bytes(udf_bytes)
    
    generator = UYAPPDFGenerator()
    if mode == 'birebir':
        return generator.create_pdf_birebir(parsed, output_path, udf_bytes=udf_bytes)
    return generator.create_pdf(parsed, output_path)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('input', help='Input UDF file')
    parser.add_argument('output', nargs='?', help='Output PDF file')
    parser.add_argument('--mode', choices=['birebir', 'structured'],
                        default='birebir',
                        help='PDF rendering mode (default: birebir)')
    args = parser.parse_args()
    
    input_path = Path(args.input)
    if not input_path.exists():
        print(f'Input file not found: {input_path}', file=sys.stderr)
        return 2
    
    output_path = args.output or str(input_path.with_suffix('.pdf').name.replace('.pdf', '_generated.pdf'))
    output_path = Path('outputs') / Path(output_path).name
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    pdf_bytes = generate_pdf_from_udf(str(input_path), str(output_path),
                                       mode=args.mode)
    print(f'Generated PDF ({args.mode}): {output_path} ({len(pdf_bytes)} bytes)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
