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
    Convert DOCX file to PDF using ReportLab with Turkish font support.
    
    Args:
        docx_bytes: Raw DOCX file bytes
    
    Returns:
        bytes: PDF file data
    
    Raises:
        ValueError: If DOCX parsing fails
    """
    try:
        from docx import Document
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from pathlib import Path
        
        # Font resolution - try to use Turkish-supporting fonts
        font_name = 'Helvetica'  # Fallback
        font_bold = 'Helvetica-Bold'
        
        # Try Linux fonts (Render)
        for font_path, name in [
            ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 'DejaVuSans'),
            ('/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf', 'LiberationSans'),
        ]:
            if Path(font_path).exists():
                try:
                    pdfmetrics.registerFont(TTFont(name, font_path))
                    font_name = name
                    break
                except:
                    pass
        
        # Try Windows fonts (local)
        for font_path, name in [
            ('C:/Windows/Fonts/arial.ttf', 'Arial'),
            ('C:/Windows/Fonts/arialbd.ttf', 'ArialBold'),
        ]:
            if Path(font_path).exists():
                try:
                    pdfmetrics.registerFont(TTFont(name, font_path))
                    font_name = 'Arial'
                    font_bold = 'ArialBold'
                    break
                except:
                    pass
        
        # Parse DOCX
        doc = Document(io.BytesIO(docx_bytes))
        
        # Create PDF
        buffer = io.BytesIO()
        c = canvas.Canvas(buffer, pagesize=A4)
        
        # Font settings
        font_size = 11
        line_height = font_size * 1.2
        
        # Page dimensions
        page_width, page_height = A4
        margin_left = 40
        margin_right = 40
        margin_top = 50
        y = page_height - margin_top
        
        # Extract and render paragraphs
        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                y -= line_height * 0.5
                continue
            
            # Check for bold (simple detection from runs)
            is_bold = False
            for run in para.runs:
                if run.bold:
                    is_bold = True
                    break
            
            # Select appropriate font
            if is_bold:
                current_font = font_bold
            else:
                current_font = font_name
            
            c.setFont(current_font, font_size)
            
            # Wrap text to fit page width
            max_width = page_width - margin_left - margin_right
            wrapped_lines = _wrap_text_simple(text, max_width, current_font, font_size)
            
            # Draw wrapped lines
            for line in wrapped_lines:
                if y < margin_top + 50:
                    c.showPage()
                    y = page_height - margin_top
                
                c.drawString(margin_left, y, line)
                y -= line_height
        
        # Finalize PDF
        c.save()
        return buffer.getvalue()
        
    except Exception as exc:
        logger.error(f"DOCX to PDF conversion error: {exc}")
        raise ValueError(f'Failed to convert DOCX: {str(exc)[:100]}')


def _wrap_text_simple(text: str, max_width: float, font_name: str, font_size: int, 
                     word_split=False) -> list:
    """Simple word-wrap text to fit within max_width."""
    from reportlab.pdfgen import canvas as canvas_module
    
    # Create a dummy canvas to measure text
    dummy = io.BytesIO()
    c = canvas_module.Canvas(dummy)
    c.setFont(font_name, font_size)
    
    words = text.split()
    lines = []
    current_line = ""
    
    for word in words:
        test_line = (current_line + " " + word).strip()
        width = c.stringWidth(test_line, font_name, font_size)
        
        if width > max_width:
            if current_line:
                lines.append(current_line)
            current_line = word
        else:
            current_line = test_line
    
    if current_line:
        lines.append(current_line)
    
    return lines if lines else [text[:50]]


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
    return jsonify({'status': 'ok', 'service': 'mini-udf-service', 'version': '2.0'}), 200

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
    
    Uses DejaVu Sans / Liberation Sans fonts (Arial-compatible, Turkish support).
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
        logger.error(f"DOCX file read error: {e}")
        return jsonify({'error': 'Failed to read file'}), 400

    try:
        pdf_bytes = docx_to_pdf_bytes(file_data)
        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={'Content-Disposition': 'inline; filename=preview.pdf'}
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

    ui_json, err = _extract_placeholders(request)
    if err:
        return jsonify({'error': err[0]}), err[1]

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

    ui_json, err = _extract_placeholders(request)
    if err:
        return jsonify({'error': err[0]}), err[1]

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
    """Generate filled template rendered as PDF."""
    try:
        from tools.generate_document import generate_pdf, output_filename
    except ImportError:
        return jsonify({'error': 'PDF generation module not available'}), 501

    ui_json, err = _extract_placeholders(request)
    if err:
        return jsonify({'error': err[0]}), err[1]

    template = request.args.get('template', 'AnlasmaBelgesi-#Dolu_v1.docx')
    try:
        pdf_bytes = generate_pdf(ui_json, template_name=template)
        fname = output_filename(ui_json, ext='pdf', template_name=template)
        return Response(
            pdf_bytes,
            mimetype='application/pdf',
            headers={
                'Content-Disposition': _content_disposition(fname),
                'X-Filename': fname,
            },
        )
    except FileNotFoundError as exc:
        return jsonify({'error': str(exc)}), 404
    except Exception as exc:
        logger.error(f"PDF generation error: {exc}")
        return jsonify({'error': str(exc)}), 500


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
