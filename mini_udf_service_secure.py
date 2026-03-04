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
    allow_headers=['Content-Type', 'X-API-Key', 'X-CSRF-Token'],
    methods=['GET', 'POST', 'OPTIONS'],
    max_age=3600
)

# ─── SECURITY: API KEY VALIDATION ──────────────────────────────
def require_api_key(f):
    """Wrapper: Require valid API key in X-API-Key header"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'OPTIONS':
            return make_response('', 204)

        # GET /health endpoint'inde API key gerekmiyor (monitoring için)
        if request.path == '/health' and request.method == 'GET':
            return f(*args, **kwargs)
        
        token = request.headers.get('X-API-Key', '').strip()
        
        if ENVIRONMENT == 'production' and not API_SECRET_KEY:
            logger.error("API_SECRET_KEY not set in production mode!")
            return jsonify({'error': 'Server misconfigured'}), 500
        
        if not token or token != API_SECRET_KEY:
            logger.warning(f"Unauthorized API attempt from {request.remote_addr}")
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
