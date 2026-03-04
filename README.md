# 🔐 Mini UDF Service - Production Edition

**Güvenli, ölçeklenebilir UDF parsing microservice**

## ✨ Özellikler

✅ **Güvenlik**
- API Key authentication (X-API-Key header)
- Rate limiting (10 req/min per IP)
- File size validation (50MB max)
- CORS whitelist
- No stack traces exposed
- HTTPS support

✅ **Performans**
- Gunicorn multi-worker support (4 workers × 2 threads = 8 concurrent requests)
- Async file processing
- Memory efficient streaming
- Connection pooling

✅ **Production-Ready**
- Structured logging
- Health check endpoints
- Detailed configuration
- Docker support
- Nginx reverse proxy config

---

## 🚀 Hızlı Başlangıç

### 1. Kurulum

```bash
# Depo'yu kopyala
cd production

# Python 3.9+ gereklidir
python --version

# Gerekli paketleri yükle
pip install -r requirements-prod.txt
```

### 2. Konfigürasyon

```bash
# Template'i kopyala
cp .env.example .env
```

**`.env` dosyasında şu değerleri güncelleyin:**

```env
# Kritik: Güçlü API key oluşturun
API_SECRET_KEY=sk_live_abc123xyz...

# CORS whitelist (kendi domain'iniz)
CORS_ORIGINS=https://yourdomain.com

# Ortam
ENVIRONMENT=production
```

**API Key üretyn:**
```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

### 3. Başlatma

#### Linux/macOS:
```bash
chmod +x start_production.sh
./start_production.sh
```

#### Windows (PowerShell):
```powershell
.\start_production.ps1
```

#### Manuel (herhangi bir OS):
```bash
gunicorn -w 4 --threads 2 -b 0.0.0.0:5055 mini_udf_service_secure:app
```

---

## 📡 API Endpoints

### 1. Health Check
```bash
curl http://localhost:5055/health
```

**Response:**
```json
{
  "status": "ok",
  "service": "mini-udf-service",
  "version": "2.0"
}
```

### 2. UDF Parse (Requires Auth)
```bash
curl -X POST \
  -H "X-API-Key: sk_live_abc123xyz..." \
  -F "file=@sample.udf" \
  http://localhost:5055/api/parse-udf
```

**Response:**
```json
{
  "fields": {
    "adi_soyadi": "Ahmet Yılmaz",
    "tc_kimlik_no": "12345678901",
    ...
  },
  "confidences": {...},
  "warnings": [...],
  "validations": {...},
  "metadata": {...}
}
```

**Hata Response:**
```json
{
  "error": "Unauthorized"  // 401
}
// veya
{
  "error": "File size exceeds limit"  // 413
}
// veya
{
  "error": "Too many requests"  // 429
}
```

### 3. Detailed Health Check (Requires Auth)
```bash
curl -H "X-API-Key: sk_live_abc123xyz..." \
  http://localhost:5055/api/health-detailed
```

**Response:**
```json
{
  "status": "ok",
  "memory_mb": 145.23,
  "cpu_percent": 12.5,
  "environment": "production"
}
```

---

## 🔧 Deployment

### Seçenek 1: Linux/macOS (Recommended)

```bash
# Systemd service oluştur
sudo nano /etc/systemd/system/mini-udf.service
```

```ini
[Unit]
Description=Mini UDF Service
After=network.target

[Service]
Type=notify
User=www-data
WorkingDirectory=/opt/mini-udf-service
ExecStart=/opt/mini-udf-service/start_production.sh
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
```

```bash
sudo chmod +x /opt/mini-udf-service/start_production.sh
sudo systemctl daemon-reload
sudo systemctl enable mini-udf.service
sudo systemctl start mini-udf.service
```

### Seçenek 2: Docker

```bash
# Build
docker build -t mini-udf-service:latest .

# Run
docker run -d \
  --name mini-udf \
  -p 5055:5055 \
  --env-file .env \
  -v $(pwd)/logs:/app/logs \
  mini-udf-service:latest
```

### Seçenek 3: Nginx Reverse Proxy

```bash
# Nginx config'ini kopyala
sudo cp nginx.conf /etc/nginx/sites-available/mini-udf-service
sudo nano /etc/nginx/sites-available/mini-udf-service
# YOUR_DOMAIN_HERE'i değiştir

# Etkinleştir
sudo ln -s /etc/nginx/sites-available/mini-udf-service /etc/nginx/sites-enabled/

# Test et
sudo nginx -t

# SSL sertifikası oluştur (Let's Encrypt)
sudo certbot certonly --nginx -d yourdomain.com

# Nginx'i restart et
sudo systemctl reload nginx
```

---

## 📊 Performans Tuning

### Gunicorn Ayarları

```bash
# High Traffic (1000+ req/s)
gunicorn -w 8 --threads 4 -b 0.0.0.0:5055 --timeout 60

# Medium Traffic (100-1000 req/s)
gunicorn -w 4 --threads 2 -b 0.0.0.0:5055 --timeout 30

# Low Traffic (< 100 req/s)
gunicorn -w 2 --threads 1 -b 0.0.0.0:5055
```

### CPU / Memory
- Per worker: ~100-150 MB
- 4 workers: ~400-600 MB
- Concurrent requests: workers × threads

### Redis Rate Limiting (Production)

In-memory limiter yerine Redis kullan (distributed systems için):

1. Redis kur:
```bash
sudo apt install redis-server
sudo systemctl start redis-server
```

2. `.env` güncelle:
```env
REDIS_URL=redis://localhost:6379/0
```

3. Kodu güncelle:
```python
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    storage_uri=os.getenv('REDIS_URL', 'memory://')
)
```

---

## 🛡️ Güvenlik Checklist

- [ ] `.env` dosyası `.gitignore`'a eklendi
- [ ] `API_SECRET_KEY` güçlü ve secret (64+ karakter)
- [ ] `CORS_ORIGINS` whitelist yapılandırıldı
- [ ] HTTPS/SSL sertifikası yapılandırıldı
- [ ] Nginx rate limiting aktif
- [ ] Firewall kuralları uygulandı (5055 port'u restrictive)
- [ ] Logs düzenli olarak kontrol ediliyor
- [ ] Backup stratejisi belirlendi
- [ ] WAF (ModSecurity) opsiyonel olarak ekle

---

## 📝 Logging

Logs otomatik olarak `logs/app.log`'a yazılır.

Gerçek-zamanlı log görüntüle:
```bash
tail -f logs/app.log
```

Log levels production'da WARNING, development'da INFO.

---

## 🔍 Monitoring

### Health Check
```bash
# Cron job'a ekle (her 5 dakika)
*/5 * * * * curl -s http://localhost:5055/health | grep -q '"status":"ok"' || notify-admin
```

### Custom Metrics
```bash
curl -H "X-API-Key: YOUR_KEY" \
  http://localhost:5055/api/health-detailed
```

---

## 🐛 Troubleshooting

### "Unauthorized" (401)
```bash
# API Key check
echo $API_SECRET_KEY
# Backend'de kontrol
grep API_SECRET_KEY .env
```

### "Too many requests" (429)
```bash
# Rate limit artır (50 -> 100)
# .env'de: GUNICORN_WORKERS=8
./start_production.sh
```

### Memory leak
```bash
# Worker'ları restart et
# .env'de: GUNICORN_MAX_REQUESTS=1000
gunicorn ... --max-requests 1000
```

---

## 📚 CLI Usage

UDF'i parse etmek için CLI mode:

```bash
python mini_udf_service_secure.py --file sample.udf
```

Output:
```json
{
  "fields": {...},
  "confidences": {...}
}
```

---

## 🔄 Updates

Kod güncellemeleri için:
```bash
cd production
git pull
pip install -r requirements-prod.txt --upgrade
sudo systemctl restart mini-udf
```

---

## 📞 Support

- Documentation: Bk. bu dosya
- Issues: GitHub Issues
- Security: security@yourdomain.com

---

## 📄 License

Proprietary - Tüm hakları saklıdır.

---

**Son güncelleme:** 2026-03-04
**Version:** 2.0-secure
