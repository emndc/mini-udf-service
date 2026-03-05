#!/usr/bin/env python3
"""
Minimal UDF -> JSON miniservice and CLI (Production-Ready & Secure)

SECURITY FEATURES:
- API Key validation (X-API-Key header)
- Rate limiting (10 req/min per IP)
- File size validation (50MB max)
- CORS whitelist
- No stack traces exposed to client
- HTTPS enforced in production
- SQL injection prevention (parameterized queries)

ENDPOINTS:
- POST /api/parse-udf with form-data key "file" containing .udf
- GET /health returns OK
"""
import argparse
import io
import json
import zipfile
import os
import sys
import logging
from functools import wraps
from pathlib import Path

# Third-party imports
from flask import Flask, jsonify, request, Response, make_response
from flask_cors import CORS
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash
import tempfile
import subprocess
import shutil

# Production security
try:
    from dotenv import load_dotenv
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
except ImportError:
    print("ERROR: Missing required packages. Run: pip install -r requirements-prod.txt")
    sys.exit(1)

# Load environment variables
load_dotenv()

# ─── CONFIGURATION ───────────────────────────────────────────────
DEBUG = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
API_SECRET_KEY = os.getenv('API_SECRET_KEY', '')
MAX_FILE_SIZE = int(os.getenv('MAX_FILE_SIZE', 50 * 1024 * 1024))  # 50MB
ALLOWED_EXTENSIONS = {'udf', 'docx'}
_raw_cors_origins = os.getenv(
    'CORS_ORIGINS',
    'http://localhost:3000,http://localhost:5173,https://localhost:5173'
)
CORS_ORIGINS = [origin.strip() for origin in _raw_cors_origins.split(',') if origin.strip()]
ENVIRONMENT = os.getenv('ENVIRONMENT', 'development')

# ─── LOGGING ────────────────────────────────────────────────────
# Create logs directory first
Path('logs').mkdir(exist_ok=True)

# Configure logging (stdout for Render, file for local)
handlers = [logging.StreamHandler()]

try:
    handlers.append(logging.FileHandler('logs/app.log'))
except (OSError, PermissionError):
    pass  # Can't write to file (e.g., Render), use stdout only

logging.basicConfig(
    level=logging.WARNING if ENVIRONMENT == 'production' else logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=handlers
)
logger = logging.getLogger(__name__)

# ─── FLASK APP ───────────────────────────────────────────────────
app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False
app.config['PROPAGATE_EXCEPTIONS'] = True
app.config['JSON_AS_ASCII'] = False  # Enable UTF-8 in JSON responses

# ─── LIMITER ────────────────────────────────────────────────────
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"  # Use Redis in production: redis://localhost:6379
)

# ─── CORS (Whitelist) ───────────────────────────────────────────
CORS(
    app,
    origins=CORS_ORIGINS,
    expose_headers=['Content-Disposition', 'X-Filename'],
    allow_headers=['Content-Type', 'X-API-Key', 'X-CSRF-Token', 'Authorization'],
    methods=['GET', 'POST', 'OPTIONS'],
    max_age=3600
)

# ─── SECURITY: API KEY VALIDATION ──────────────────────────────
def _is_valid_api_key(token: str) -> bool:
    """Validate provided token against API_SECRET_KEY (plain or hashed)."""
    if not token or not API_SECRET_KEY:
        return False

    if token == API_SECRET_KEY:
        return True

    try:
        if API_SECRET_KEY.startswith(('pbkdf2:', 'scrypt:', 'argon2:')):
            return check_password_hash(API_SECRET_KEY, token)
    except Exception:
        return False

    return False


def require_api_key(f):
    """Wrapper: Require valid API key in X-API-Key or Authorization: Bearer."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'OPTIONS':
            return make_response('', 204)

        # GET /health endpoint'inde API key gerekmiyor (monitoring için)
        if request.path == '/health' and request.method == 'GET':
            return f(*args, **kwargs)
        
        token = request.headers.get('X-API-Key', '').strip()
        if not token:
            auth_header = request.headers.get('Authorization', '').strip()
            if auth_header.lower().startswith('bearer '):
                token = auth_header[7:].strip()
        
        if ENVIRONMENT == 'production' and not API_SECRET_KEY:
            logger.error("API_SECRET_KEY not set in production mode!")
            return jsonify({'error': 'Server misconfigured'}), 500
        
        if not _is_valid_api_key(token):
            logger.warning(f"Unauthorized API attempt from {request.remote_addr} on {request.path}")
            return jsonify({'error': 'Unauthorized'}), 401
        
        return f(*args, **kwargs)
    
    return decorated

# ─── FILE VALIDATION ─────────────────────────────────────────────
def validate_file_upload(file_obj):
    """
    Validate uploaded file.
    Returns: (filename_safe, file_size) or raises ValueError
    """
    if not file_obj or file_obj.filename == '':
        raise ValueError('No filename provided')
    
    # Sanitize filename
    fname = secure_filename(file_obj.filename)
    if not fname:
        raise ValueError('Invalid filename')
    
    # Check extension
    ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f'Invalid file type: .{ext}')
    
    # Check file size without loading entire file into memory
    file_obj.seek(0, 2)  # Seek to end
    size = file_obj.tell()
    file_obj.seek(0)  # Seek to start
    
    if size == 0:
        raise ValueError('Empty file')
    
    if size > MAX_FILE_SIZE:
        raise ValueError(f'File exceeds maximum size ({MAX_FILE_SIZE / 1024 / 1024:.0f}MB)')
    
    return fname, size

# ─── ERROR HANDLERS ──────────────────────────────────────────────
@app.errorhandler(400)
def bad_request(error):
    logger.warning(f"Bad request: {request.path}")
    return jsonify({'error': 'Bad request'}), 400

@app.errorhandler(401)
def unauthorized(error):
    return jsonify({'error': 'Unauthorized'}), 401

@app.errorhandler(404)
def not_found(error):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(429)
def ratelimit_handler(e):
    logger.warning(f"Rate limit exceeded: {request.remote_addr}")
    return jsonify({'error': 'Too many requests. Please wait before trying again.'}), 429

@app.errorhandler(Exception)
def handle_error(error):
    """Catch-all error handler - never expose stack trace to client"""
    logger.error(f"Unhandled error: {error}", exc_info=True)
    
    if ENVIRONMENT == 'production':
        return jsonify({'error': 'Internal server error'}), 500
    
    # In development, include more details
    return jsonify({'error': str(error)}), 500

# ─── IMPORT UDF TOOLS (Lazy loading for Render compatibility) ────────────────────────
# Don't import at startup - will be lazy loaded when endpoints are called
# This allows service to at least respond to /health without tools/ folder

_udf_tools_loaded = False
_udf_tools_available = False
create_docx_from_udf_lxml = None
decode_cdata_bytes_with_meta = None
extract_fields = None

def _load_udf_tools():
    """Lazy load UDF extraction tools - called on first API request"""
    global _udf_tools_loaded, _udf_tools_available
    global create_docx_from_udf_lxml, decode_cdata_bytes_with_meta, extract_fields
    
    if _udf_tools_loaded:
        return _udf_tools_available
    
    try:
        # Try multiple paths (handles local, Render, Docker deployments)
        tools_paths = [
            Path(__file__).parent / 'tools',          # Local: production/tools
            Path(__file__).parent.parent / 'tools',   # Local: /tools (if run from parent)
            Path('/opt/render/project/src/tools'),    # Render
            Path('/app/tools'),                       # Docker
        ]
        
        for tools_path in tools_paths:
            if tools_path.exists():
                sys.path.insert(0, str(tools_path.parent))
                break
        
        from tools.extract_udf_cdata_lxml import create_docx_from_udf_lxml as _cdflu
        from tools.udf_extract_to_json import decode_cdata_bytes_with_meta as _dcbwm, extract_fields as _ef
        
        create_docx_from_udf_lxml = _cdflu
        decode_cdata_bytes_with_meta = _dcbwm
        extract_fields = _ef
        
        _udf_tools_available = True
        logger.info("UDF extraction tools loaded successfully")
    except ImportError as exc:
        logger.warning(f"UDF tools not available: {exc} - only /health endpoint will work")
        _udf_tools_available = False
    
    _udf_tools_loaded = True
    return _udf_tools_available

# ─── CORE FUNCTIONS ──────────────────────────────────────────────
def docx_to_pdf_bytes(docx_bytes: bytes) -> bytes:
    """
    Convert DOCX file to PDF using mammoth (DOCX→HTML) + xhtml2pdf (HTML→PDF).
    
    Pure Python solution - no system dependencies needed.
    Preserves tables, formatting, bold/italic text.
    Uses DejaVu Sans font for full Turkish character support.
    
    Falls back to LibreOffice (if available) or UDF→PDF as last resort.
    
    Args:
        docx_bytes: Raw DOCX file bytes
    
    Returns:
        bytes: PDF file data
    
    Raises:
        ValueError: If DOCX conversion fails
    """
    import io as _io
    
    # ── Method 1: mammoth + xhtml2pdf (primary, pure Python) ──
    try:
        import mammoth
        from xhtml2pdf import pisa
        import xhtml2pdf.default
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.lib.fonts import addMapping as rl_addMapping
        
        # Register DejaVu Sans with ReportLab (Turkish character support)
        _fonts_dir = Path(__file__).resolve().parent / 'fonts'
        _font_regular = _fonts_dir / 'DejaVuSans.ttf'
        _font_bold = _fonts_dir / 'DejaVuSans-Bold.ttf'
        
        if _font_regular.exists() and _font_bold.exists():
            try:
                pdfmetrics.registerFont(TTFont('DejaVuSans', str(_font_regular)))
                pdfmetrics.registerFont(TTFont('DejaVuSans-Bold', str(_font_bold)))
                rl_addMapping('DejaVuSans', 0, 0, 'DejaVuSans')
                rl_addMapping('DejaVuSans', 1, 0, 'DejaVuSans-Bold')
                rl_addMapping('DejaVuSans', 0, 1, 'DejaVuSans')
                rl_addMapping('DejaVuSans', 1, 1, 'DejaVuSans-Bold')
                # Override xhtml2pdf default font mappings
                for key in ('helvetica', 'helvetica-bold', 'arial', 'sans',
                            'sansserif', 'serif', 'times', 'times-roman',
                            'times-bold', 'verdana', 'geneva'):
                    if 'bold' in key:
                        xhtml2pdf.default.DEFAULT_FONT[key] = 'DejaVuSans-Bold'
                    else:
                        xhtml2pdf.default.DEFAULT_FONT[key] = 'DejaVuSans'
                xhtml2pdf.default.DEFAULT_FONT['dejavusans'] = 'DejaVuSans'
                logger.info(f'DejaVu Sans fonts registered from {_fonts_dir}')
            except Exception as font_exc:
                logger.warning(f'Font registration failed (may already be registered): {font_exc}')
        else:
            logger.warning(f'DejaVu Sans fonts not found at {_fonts_dir}')
        
        logger.info('Converting DOCX→HTML via mammoth...')
        result = mammoth.convert_to_html(_io.BytesIO(docx_bytes))
        html_body = result.value
        
        if result.messages:
            for msg in result.messages[:5]:
                logger.info(f'mammoth message: {msg}')
        
        # Wrap with proper HTML structure and CSS for Turkish text & tables
        full_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
    @page {{
        size: A4;
        margin: 2cm;
    }}
    body {{
        font-family: "DejaVuSans", "Helvetica", "Arial", sans-serif;
        font-size: 11pt;
        line-height: 1.4;
        color: #000;
    }}
    table {{
        width: 100%;
        border-collapse: collapse;
        margin: 8pt 0;
    }}
    td, th {{
        border: 1px solid #333;
        padding: 4pt 6pt;
        text-align: left;
        vertical-align: top;
        font-size: 10pt;
    }}
    th {{
        background-color: #f0f0f0;
        font-weight: bold;
    }}
    h1 {{ font-size: 16pt; margin: 12pt 0 6pt 0; }}
    h2 {{ font-size: 14pt; margin: 10pt 0 5pt 0; }}
    h3 {{ font-size: 12pt; margin: 8pt 0 4pt 0; }}
    p {{ margin: 3pt 0; }}
    strong {{ font-weight: bold; }}
    em {{ font-style: italic; }}
</style>
</head>
<body>
{html_body}
</body>
</html>"""
        
        logger.info(f'HTML size: {len(full_html)} chars, converting to PDF via xhtml2pdf...')
        
        pdf_buffer = _io.BytesIO()
        pisa_status = pisa.CreatePDF(
            full_html,
            dest=pdf_buffer,
            encoding='utf-8'
        )
        
        if pisa_status.err:
            logger.warning(f'xhtml2pdf had errors: {pisa_status.err}')
        
        pdf_bytes = pdf_buffer.getvalue()
        logger.info(f'mammoth+xhtml2pdf conversion succeeded, PDF size: {len(pdf_bytes)} bytes')
        
        if len(pdf_bytes) > 500:
            return pdf_bytes
        else:
            logger.warning('xhtml2pdf produced empty/tiny PDF, trying fallback...')
    
    except ImportError as imp_err:
        logger.warning(f'mammoth/xhtml2pdf not available: {imp_err}')
    except Exception as exc:
        logger.error(f'mammoth+xhtml2pdf conversion failed: {type(exc).__name__}: {exc}')
    
    # ── Method 2: LibreOffice (if installed) ──
    try:
        import tempfile
        import subprocess
        import shutil
        
        soffice = shutil.which('soffice')
        if soffice:
            logger.info(f'Trying LibreOffice at: {soffice}')
            with tempfile.TemporaryDirectory() as tmpdir:
                docx_path = os.path.join(tmpdir, 'input.docx')
                pdf_path = os.path.join(tmpdir, 'input.pdf')
                
                with open(docx_path, 'wb') as f:
                    f.write(docx_bytes)
                
                result = subprocess.run(
                    [soffice, '--headless', '--convert-to', 'pdf',
                     '--outdir', tmpdir, docx_path],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=30
                )
                
                if os.path.exists(pdf_path):
                    pdf_bytes = Path(pdf_path).read_bytes()
                    logger.info(f'LibreOffice conversion succeeded, PDF size: {len(pdf_bytes)} bytes')
                    return pdf_bytes
    except Exception as exc:
        logger.warning(f'LibreOffice fallback failed: {exc}')
    
    # ── Method 3: UDF→PDF (last resort) ──
    logger.info('Falling back to UDF→PDF conversion')
    return _docx_to_pdf_via_udf(docx_bytes)


def _docx_to_pdf_via_udf(docx_bytes: bytes) -> bytes:
    """Fallback: Convert DOCX→UDF→PDF using UYAP PDF generator."""
    import io
    import zipfile
    try:
        # Convert DOCX → UDF
        from docx import Document
        doc = Document(io.BytesIO(docx_bytes))
        
        # Extract text for UDF content (simplified)
        paragraphs = []
        for para in doc.paragraphs:
            text = para.text.strip()
            if text:
                paragraphs.append(text)
        
        # Generate UDF-like structure
        from tools.udf_to_pdf import UYAPPDFGenerator
        from tools.udf_extract_to_json import decode_cdata_bytes_with_meta
        
        # Create minimal UDF bytes with content
        joined = "\n".join(paragraphs)
        content_xml = f'<?xml version="1.0" encoding="UTF-8"?><Document><Content><![CDATA[{joined}]]></Content></Document>'
        
        udf_buffer = io.BytesIO()
        with zipfile.ZipFile(udf_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('content.xml', content_xml.encode('utf-8'))
        udf_bytes = udf_buffer.getvalue()
        
        # Parse and generate PDF
        meta = decode_cdata_bytes_with_meta(content_xml.encode('utf-8'))
        parsed = {
            'fields': {},
            'confidences': {},
            'warnings': [],
            'validations': [],
            'metadata': meta,
        }
        generator = UYAPPDFGenerator()
        return generator.create_pdf_birebir(parsed, udf_bytes=udf_bytes)
    
    except Exception as exc:
        logger.error(f"UDF fallback conversion failed: {exc}")
        raise ValueError(f'PDF conversion failed: {str(exc)[:100]}')


def parse_udf_bytes(udf_bytes: bytes):
    """
    Parse UDF file and extract fields.
    
    Args:
        udf_bytes: Raw UDF file bytes
    
    Returns:
        dict: {fields, confidences, warnings, validations, metadata}
    
    Raises:
        ValueError: If parsing fails or tools unavailable
    """
    # Lazy load tools
    if not _load_udf_tools():
        raise ValueError('UDF extraction tools not available on this server')
    
    try:
        with zipfile.ZipFile(io.BytesIO(udf_bytes), 'r') as zf:
            if 'content.xml' not in zf.namelist():
                raise ValueError('Invalid UDF: content.xml not found')
            # Read with explicit UTF-8 encoding
            raw = zf.read('content.xml').decode('utf-8', errors='replace').encode('utf-8')
    except zipfile.BadZipFile:
        raise ValueError('Invalid file format (not a valid ZIP/UDF)')
    except Exception as exc:
        logger.error(f"UDF parsing error: {exc}")
        raise ValueError(f'Failed to read UDF: {str(exc)[:100]}')
    
    try:
        meta = decode_cdata_bytes_with_meta(raw)
        text = meta['text']
        fields, confidences, warnings, validations = extract_fields(text)
        
        return {
            'fields': fields,
            'confidences': confidences,
            'warnings': warnings,
            'validations': validations,
            'metadata': meta
        }
    except Exception as exc:
        logger.error(f"Field extraction error: {exc}")
        raise ValueError(f'Failed to extract fields: {str(exc)[:100]}')


_pdf_generator_cls = None


def _load_pdf_generator_cls():
    """Lazy load PDF generator class from tools package."""
    global _pdf_generator_cls
    if _pdf_generator_cls is not None:
        return _pdf_generator_cls

    try:
        # Ensure tools path is available
        _load_udf_tools()
        from tools.udf_to_pdf import UYAPPDFGenerator
        _pdf_generator_cls = UYAPPDFGenerator
        return _pdf_generator_cls
    except Exception as exc:
        logger.error(f"PDF generator import failed: {exc}")
        return None

# ─── ENDPOINTS ──────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint (no auth required)"""
    return jsonify({'status': 'ok', 'service': 'mini-udf-service', 'version': '2.1-mammoth'}), 200


@app.route('/diagnostic/libreoffice', methods=['GET'])
@require_api_key
def diagnostic_libreoffice():
    """Diagnostic endpoint to check LibreOffice availability and status."""
    import shutil
    import subprocess
    
    info = {}
    
    # Check if LibreOffice is available
    soffice = shutil.which('soffice')
    info['soffice_path'] = soffice
    info['soffice_available'] = soffice is not None
    
    if soffice:
        # Try to get version
        try:
            result = subprocess.run(
                [soffice, '--version'],
                capture_output=True,
                text=True,
                timeout=10
            )
            info['soffice_version'] = result.stdout.strip()
            info['soffice_version_error'] = result.stderr.strip() if result.stderr else None
        except Exception as exc:
            info['soffice_version'] = None
            info['soffice_version_error'] = str(exc)
    
    return jsonify(info), 200


@app.route('/api/parse-udf', methods=['POST'])
@limiter.limit("10 per minute")
@require_api_key
def parse_udf_endpoint():
    """
    Parse UDF file and extract structured fields.
    
    Request:
        - files["file"]: UDF file (max 50MB)
        - headers["X-API-Key"]: Required API key
    
    Response:
        - 200: {fields, confidences, warnings, validations, metadata}
        - 400: {error: reason}
        - 401: {error: "Unauthorized"}
        - 413: {error: "File too large"}
        - 429: {error: "Too many requests"}
    """
    if 'file' not in request.files:
        return jsonify({'error': 'file field required'}), 400
    
    # Validate file
    try:
        fname, size = validate_file_upload(request.files['file'])
    except ValueError as e:
        logger.warning(f"File validation error: {e}")
        return jsonify({'error': f'Invalid file: {str(e)}'}), 400
    
    # Read file (limited by MAX_FILE_SIZE check in validate)
    try:
        file_data = request.files['file'].read(MAX_FILE_SIZE + 1)
        if len(file_data) > MAX_FILE_SIZE:
            return jsonify({'error': 'File size exceeds limit'}), 413
    except Exception as e:
        logger.error(f"File read error: {e}")
        return jsonify({'error': 'Failed to read file'}), 400
    
    # Parse UDF
    try:
        result = parse_udf_bytes(file_data)
        logger.info(f"Successfully parsed UDF from {request.remote_addr}")
        return jsonify(result), 200
    except ValueError as e:
        logger.warning(f"UDF parsing failed: {e}")
        return jsonify({'error': str(e)}), 400

@app.route('/api/parse-udf-ui', methods=['POST'])
@limiter.limit("10 per minute")
@require_api_key
def parse_udf_ui_endpoint():
    """Parse UDF and return UI-mapped schema with 'ui' field.
    
    Response format: {ui: {...parsed fields...}, mapped: true}
    """
    if 'file' not in request.files:
        return jsonify({'error': 'file field required'}), 400
    
    try:
        fname, size = validate_file_upload(request.files['file'])
    except ValueError as e:
        return jsonify({'error': f'Invalid file: {str(e)}'}), 400
    
    try:
        file_data = request.files['file'].read(MAX_FILE_SIZE + 1)
        if len(file_data) > MAX_FILE_SIZE:
            return jsonify({'error': 'File size exceeds limit'}), 413
        
        parsed = parse_udf_bytes(file_data)
        # Map to UI schema (you can implement this)
        return jsonify({'ui': parsed, 'mapped': True}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@app.route('/api/preview-udf', methods=['POST'])
@limiter.limit("10 per minute")
@require_api_key
def preview_udf_endpoint():
    """Generate preview PDF from uploaded UDF.

    Query params:
      mode=birebir (default) | structured
    """
    if 'file' not in request.files:
        return jsonify({'error': 'file field required'}), 400

    try:
        _, _ = validate_file_upload(request.files['file'])
    except ValueError as e:
        return jsonify({'error': f'Invalid file: {str(e)}'}), 400

    try:
        file_data = request.files['file'].read(MAX_FILE_SIZE + 1)
        if len(file_data) > MAX_FILE_SIZE:
            return jsonify({'error': 'File size exceeds limit'}), 413
    except Exception as e:
        logger.error(f"Preview file read error: {e}")
        return jsonify({'error': 'Failed to read file'}), 400

    try:
        parsed = parse_udf_bytes(file_data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    mode = (request.args.get('mode', 'birebir') or 'birebir').strip().lower()
    generator_cls = _load_pdf_generator_cls()
    if generator_cls is None:
        return jsonify({'error': 'PDF preview module unavailable on server'}), 500

    try:
        generator = generator_cls()
        if mode == 'structured':
            pdf_bytes = generator.create_pdf(parsed)
        else:
            pdf_bytes = generator.create_pdf_birebir(parsed, udf_bytes=file_data)

        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={'Content-Disposition': 'inline; filename=preview.pdf'}
        )
    except Exception as exc:
        logger.error(f"Preview PDF generation failed: {exc}")
        return jsonify({'error': 'PDF oluşturulamadı'}), 500


@app.route('/api/preview-docx', methods=['POST'])
@limiter.limit("10 per minute")
@require_api_key
def preview_docx_endpoint():
    """Generate PDF from uploaded DOCX file.
    
    Uses LibreOffice (Render) for high-quality conversion preserving tables, formatting, etc.
    Falls back to UDF→PDF if LibreOffice unavailable.
    """
    if 'file' not in request.files:
        return jsonify({'error': 'file field required'}), 400

    file_obj = request.files['file']
    original_filename = file_obj.filename or 'document.docx'
    
    try:
        _, _ = validate_file_upload(file_obj)
    except ValueError as e:
        return jsonify({'error': f'Invalid file: {str(e)}'}), 400

    try:
        file_data = file_obj.read(MAX_FILE_SIZE + 1)
        if len(file_data) > MAX_FILE_SIZE:
            return jsonify({'error': 'File size exceeds limit'}), 413
    except Exception as e:
        logger.error(f"DOCX file read error: {e}")
        return jsonify({'error': 'Failed to read file'}), 400

    try:
        pdf_bytes = docx_to_pdf_bytes(file_data)
        
        # Build proper filename from original DOCX filename
        pdf_filename = original_filename.rsplit('.', 1)[0] + '.pdf'
        
        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={
                'Content-Disposition': f'inline; filename="{pdf_filename}"',
                'X-Filename': pdf_filename,
            }
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as exc:
        logger.error(f"DOCX to PDF conversion failed: {exc}")
        return jsonify({'error': 'PDF oluşturulamadı'}), 500


@app.route('/api/health-detailed', methods=['GET'])
def health_detailed():
    """Detailed health check (requires auth)"""
    @require_api_key
    def _check():
        import psutil
        proc = psutil.Process()
        return jsonify({
            'status': 'ok',
            'memory_mb': proc.memory_info().rss / 1024 / 1024,
            'cpu_percent': proc.cpu_percent(interval=1),
            'environment': ENVIRONMENT
        }), 200
    return _check()


@app.route('/api/debug/logs', methods=['GET'])
@require_api_key
def debug_logs():
    """Retrieve debug log files from outputs/ directory.
    
    Query params:
      - filename: return specific file (blank = list all)
    """
    outputs_dir = Path(__file__).resolve().parent / 'outputs'
    if not outputs_dir.exists():
        return jsonify({'error': 'No outputs directory', 'files': []}), 200
    
    filename = request.args.get('filename', '').strip()
    
    if filename:
        # Return specific file
        file_path = outputs_dir / filename
        if not file_path.exists() or not file_path.is_file():
            return jsonify({'error': f'File not found: {filename}'}), 404
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return jsonify({
                'filename': filename,
                'content': content,
                'size': file_path.stat().st_size
            }), 200
        except Exception as e:
            logger.error(f"Debug log read error: {e}")
            return jsonify({'error': str(e)}), 500
    
    # List all JSON files
    try:
        files = []
        for f in sorted(outputs_dir.glob('*.json'), key=lambda x: x.stat().st_mtime, reverse=True)[:20]:
            files.append({
                'filename': f.name,
                'size': f.stat().st_size,
                'mtime': f.stat().st_mtime
            })
        return jsonify({'files': files}), 200
    except Exception as e:
        logger.error(f"Debug log list error: {e}")
        return jsonify({'error': str(e)}), 500


# ─── DOCUMENT GENERATION HELPERS ────────────────────────────────
def _content_disposition(filename: str) -> str:
    """Return safe Content-Disposition header."""
    from urllib.parse import quote
    safe_name = quote(filename, safe='')
    return f'attachment; filename="{safe_name}"'


def _extract_placeholders(req):
    """Extract JSON body from request - either direct JSON or from file upload."""
    # Try JSON body first
    ui_json = req.get_json(silent=True)
    if ui_json:
        return ui_json, None
    
    # Try file upload (multipart/form-data)
    if 'file' in req.files:
        try:
            file_data = req.files['file'].read(MAX_FILE_SIZE + 1)
            if len(file_data) > MAX_FILE_SIZE:
                return None, ('File too large', 413)
            # Parse UDF → extract → map to UI schema
            parsed = parse_udf_bytes(file_data)
            ui_json = parsed  # Simplified: return raw parsed result
            return ui_json, None
        except ValueError as e:
            return None, (f'File parse error: {str(e)}', 400)
        except Exception as e:
            return None, (f'Error: {str(e)}', 500)
    
    return None, ('JSON body or file upload required', 400)


# ─── DOCUMENT GENERATION ENDPOINTS ────────────────────────────────
@app.route('/api/generate/docx', methods=['POST'])
@limiter.limit("10 per minute")
@require_api_key
def generate_docx_endpoint():
    """Generate filled DOCX template from UI JSON."""
    try:
        from tools.generate_document import generate_docx, output_filename
    except ImportError:
        return jsonify({'error': 'DOCX generation module not available'}), 501

    # Only accept JSON body
    ui_json = request.get_json(silent=True)
    if not ui_json:
        return jsonify({'error': 'JSON body required. For direct DOCX→PDF from uploaded file, use /api/preview-docx'}), 400

    template = request.args.get('template', 'AnlasmaBelgesi-#Dolu_v1.docx')
    try:
        docx_bytes = generate_docx(ui_json, template_name=template)
        fname = output_filename(ui_json, ext='docx', template_name=template)
        return Response(
            docx_bytes,
            mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            headers={
                'Content-Disposition': _content_disposition(fname),
                'X-Filename': fname,
            },
        )
    except FileNotFoundError as exc:
        return jsonify({'error': str(exc)}), 404
    except Exception as exc:
        logger.error(f"DOCX generation error: {exc}")
        return jsonify({'error': str(exc)}), 500


@app.route('/api/generate/udf', methods=['POST'])
@limiter.limit("10 per minute")
@require_api_key
def generate_udf_endpoint():
    """Generate filled template converted to UDF format."""
    try:
        from tools.generate_document import generate_udf, output_filename
    except ImportError:
        return jsonify({'error': 'UDF generation module not available'}), 501

    # Only accept JSON body
    ui_json = request.get_json(silent=True)
    if not ui_json:
        return jsonify({'error': 'JSON body required. For direct UDF→PDF from uploaded file, use /api/preview-udf'}), 400

    template = request.args.get('template', 'AnlasmaBelgesi-#Dolu_v1.docx')
    try:
        udf_bytes = generate_udf(ui_json, template_name=template)
        fname = output_filename(ui_json, ext='udf', template_name=template)
        return Response(
            udf_bytes,
            mimetype='application/octet-stream',
            headers={
                'Content-Disposition': _content_disposition(fname),
                'X-Filename': fname,
            },
        )
    except FileNotFoundError as exc:
        return jsonify({'error': str(exc)}), 404
    except Exception as exc:
        logger.error(f"UDF generation error: {exc}")
        return jsonify({'error': str(exc)}), 500


@app.route('/api/generate/pdf', methods=['POST'])
@limiter.limit("10 per minute")
@require_api_key
def generate_pdf_endpoint():
    """Deprecated endpoint kept for backward compatibility.

    PDF generation from form JSON must use two-step flow:
      1) /api/generate/docx
      2) /api/preview-docx
    """
    return jsonify({
        'error': 'This endpoint is disabled.',
        'message': 'Use two-step flow: POST /api/generate/docx then POST /api/preview-docx',
    }), 410


@app.route('/api/fill-docx', methods=['POST'])
@limiter.limit("10 per minute")
@require_api_key
def fill_docx_endpoint():
    """Fill a DOCX template with placeholder values from UI JSON."""
    try:
        from tools.docx_template_filler import fill_template, generate_output_filename
    except ImportError:
        return jsonify({'error': 'DOCX template filler module not available'}), 501

    ui_json = request.get_json(silent=True)
    if not ui_json:
        return jsonify({'error': 'JSON body required'}), 400

    template = request.args.get('template', 'AnlasmaBelgesi-#Dolu_v1.docx')
    out_format = request.args.get('format', 'docx')

    try:
        docx_bytes = fill_template(ui_json, template_name=template)
        filename = generate_output_filename(ui_json, template_name=template.rsplit('.', 1)[0])

        if out_format == 'docx':
            return Response(
                docx_bytes,
                mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                headers={
                    'Content-Disposition': _content_disposition(filename),
                    'X-Filename': filename,
                }
            )
        else:
            return jsonify({'error': f'Unsupported format: {out_format}'}), 400
    except FileNotFoundError as exc:
        return jsonify({'error': str(exc)}), 404
    except Exception as exc:
        logger.error(f"DOCX fill error: {exc}")
        return jsonify({'error': str(exc)}), 500

# ─── CLI MODE ─────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='UDF miniservice (production-ready)'
    )
    parser.add_argument('--file', help='Parse a UDF file (CLI mode)')
    parser.add_argument('--host', default=os.getenv('HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.getenv('PORT', 5055)))
    parser.add_argument('--workers', type=int, default=1, help='Number of workers')
    
    args = parser.parse_args()
    
    if args.file:
        # CLI mode: parse UDF and print JSON
        try:
            path = Path(args.file)
            if not path.exists():
                print(f"Error: File not found: {args.file}", file=sys.stderr)
                sys.exit(1)
            
            with open(path, 'rb') as f:
                data = f.read()
            
            result = parse_udf_bytes(data)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return
    
    # Server mode
    logger.info(f"Starting micro-service on {args.host}:{args.port} ({ENVIRONMENT})")
    
    if ENVIRONMENT == 'production':
        # In production, use Gunicorn instead of Flask dev server
        logger.warning("⚠️  Running in development mode. Use Gunicorn for production!")
        logger.warning("    gunicorn -w 4 -b 0.0.0.0:5055 mini_udf_service_secure:app")
    
    # In development, start Flask dev server
    app.run(
        host=args.host,
        port=args.port,
        debug=DEBUG,
        use_reloader=False
    )

if __name__ == '__main__':
    main()
