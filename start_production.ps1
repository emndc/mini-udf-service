# ═════════════════════════════════════════════════════════════════════
# Start Mini UDF Service (Production) - Windows PowerShell
# Usage: .\start_production.ps1
# ═════════════════════════════════════════════════════════════════════

$ErrorActionPreference = "Stop"

# Colors (Windows 10+)
$Green = @{ ForegroundColor = 'Green' }
$Red = @{ ForegroundColor = 'Red' }
$Yellow = @{ ForegroundColor = 'Yellow' }

Write-Host "Mini UDF Service - Production Startup" @Green
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Check .env file
if (-not (Test-Path ".env")) {
    Write-Host "✗ .env file not found!" @Red
    Write-Host "  Please run: Copy-Item .env.example .env"
    exit 1
}

Write-Host "✓ .env file found" @Green

# Load environment variables from .env
$envFile = Get-Content ".env" -ErrorAction Stop
foreach ($line in $envFile) {
    if ($line -and -not $line.StartsWith('#')) {
        $parts = $line -split '=', 2
        if ($parts.Count -eq 2) {
            $key = $parts[0].Trim()
            $value = $parts[1].Trim()
            if ($value.StartsWith('"') -and $value.EndsWith('"')) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            [Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }
}

Write-Host "✓ Configuration loaded" @Green

# Check API key
$apiKey = [Environment]::GetEnvironmentVariable("API_SECRET_KEY")
if ([string]::IsNullOrEmpty($apiKey) -or $apiKey -eq "your-super-secret-api-key-here-generate-strong-key") {
    Write-Host "✗ API_SECRET_KEY not configured!" @Red
    Write-Host "  Generate one: python -c 'import secrets; print(secrets.token_urlsafe(64))'"
    exit 1
}

Write-Host "✓ API key configured" @Green

# Create logs directory
if (-not (Test-Path "logs")) {
    New-Item -ItemType Directory -Path "logs" -Force | Out-Null
}

Write-Host "✓ Logs directory ready" @Green

# Check for required packages
try {
    python -c "import gunicorn" 2>&1 | Out-Null
} catch {
    Write-Host "! Installing dependencies..." @Yellow
    pip install -q -r requirements-prod.txt
}

Write-Host "✓ Dependencies ready" @Green

# Get configuration
$workers = $env:GUNICORN_WORKERS -as [int] ?? 4
$threads = $env:GUNICORN_THREADS -as [int] ?? 2
$timeout = $env:GUNICORN_TIMEOUT -as [int] ?? 30
$host = $env:HOST ?? "0.0.0.0"
$port = $env:PORT ?? "5055"

Write-Host ""
Write-Host "Starting Gunicorn with:" @Yellow
Write-Host "  Workers: $workers"
Write-Host "  Threads: $threads"
Write-Host "  Timeout: ${timeout}s"
Write-Host "  Host: ${host}:${port}"
Write-Host ""

# Start Gunicorn
try {
    python -m gunicorn `
        -w $workers `
        --threads $threads `
        -b "${host}:${port}" `
        --timeout $timeout `
        --access-logfile - `
        --error-logfile - `
        --log-level info `
        mini_udf_service_secure:app
} catch {
    Write-Host "✗ Error starting service: $_" @Red
    exit 1
}
