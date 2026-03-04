#!/usr/bin/env python3
"""
Test script for Mini UDF Service API

Usage:
    python test_api.py --api-key YOUR_KEY --file sample.udf --host http://localhost:5055
"""

import argparse
import requests
import json
import sys
from pathlib import Path
from typing import Optional, Tuple
import time

# Colors for terminal output
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    END = '\033[0m'

def print_success(msg):
    print(f"{Colors.GREEN}✓ {msg}{Colors.END}")

def print_error(msg):
    print(f"{Colors.RED}✗ {msg}{Colors.END}")

def print_info(msg):
    print(f"{Colors.BLUE}ℹ {msg}{Colors.END}")

def print_warning(msg):
    print(f"{Colors.YELLOW}⚠ {msg}{Colors.END}")

class UDFServiceTester:
    def __init__(self, host: str, api_key: Optional[str] = None):
        self.host = host.rstrip('/')
        self.api_key = api_key
        self.session = requests.Session()
        if api_key:
            self.session.headers.update({'X-API-Key': api_key})
    
    def test_health(self) -> Tuple[bool, str]:
        """Test /health endpoint"""
        try:
            resp = self.session.get(f"{self.host}/health", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return True, json.dumps(data, indent=2)
            else:
                return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            return False, str(e)
    
    def test_parse_udf(self, file_path: str) -> Tuple[bool, str]:
        """Test /api/parse-udf endpoint"""
        if not Path(file_path).exists():
            return False, f"File not found: {file_path}"
        
        if not self.api_key:
            return False, "API key required for /api/parse-udf"
        
        try:
            with open(file_path, 'rb') as f:
                files = {'file': f}
                resp = self.session.post(
                    f"{self.host}/api/parse-udf",
                    files=files,
                    timeout=30
                )
            
            if resp.status_code == 200:
                data = resp.json()
                summary = {
                    'fields_count': len(data.get('fields', {})),
                    'has_confidences': bool(data.get('confidences')),
                    'warnings': len(data.get('warnings', [])),
                    'validations': len(data.get('validations', []))
                }
                return True, json.dumps(summary, indent=2)
            else:
                return False, f"HTTP {resp.status_code}: {resp.json().get('error', resp.text[:200])}"
        except Exception as e:
            return False, str(e)
    
    def test_auth(self) -> Tuple[bool, str]:
        """Test authentication requirement"""
        try:
            # Request without API key
            resp = requests.post(
                f"{self.host}/api/parse-udf",
                files={'file': b'test'},
                timeout=5
            )
            
            if resp.status_code == 401:
                return True, "Correctly rejected request without API key"
            else:
                return False, f"Expected 401, got {resp.status_code}"
        except Exception as e:
            return False, str(e)
    
    def test_rate_limit(self, requests_count: int = 15) -> Tuple[bool, str]:
        """Test rate limiting (10 per minute)"""
        if not self.api_key:
            return False, "API key required for rate limit test"
        
        try:
            limited = False
            for i in range(requests_count):
                resp = self.session.get(f"{self.host}/health")
                if resp.status_code == 429:
                    limited = True
                    break
                time.sleep(0.1)
            
            if limited:
                return True, f"Rate limit triggered after {i} requests (expected)"
            else:
                return False, f"Rate limit not triggered after {requests_count} requests"
        except Exception as e:
            return False, str(e)
    
    def test_file_size_limit(self) -> Tuple[bool, str]:
        """Test file size limit (50MB)"""
        if not self.api_key:
            return False, "API key required"
        
        try:
            # Create 60MB fake file
            fake_file = b'X' * (60 * 1024 * 1024)
            files = {'file': ('huge.udf', fake_file)}
            
            resp = self.session.post(
                f"{self.host}/api/parse-udf",
                files=files,
                timeout=10
            )
            
            if resp.status_code == 413:
                return True, "Correctly rejected oversized file (413)"
            else:
                return False, f"Expected 413, got {resp.status_code}"
        except Exception as e:
            return False, str(e)

def main():
    parser = argparse.ArgumentParser(
        description='Test Mini UDF Service API'
    )
    parser.add_argument('--host', default='http://localhost:5055',
                       help='Service host URL')
    parser.add_argument('--api-key', help='API key for authentication')
    parser.add_argument('--file', help='UDF file to test')
    parser.add_argument('--test', choices=['all', 'health', 'auth', 'parse', 'ratelimit', 'filesize'],
                       default='all', help='Which test to run')
    
    args = parser.parse_args()
    
    print(f"\n{Colors.BLUE}═══════════════════════════════════════════════════{Colors.END}")
    print(f"{Colors.BLUE}Mini UDF Service - API Test{Colors.END}")
    print(f"{Colors.BLUE}═══════════════════════════════════════════════════{Colors.END}\n")
    
    print_info(f"Testing: {args.host}")
    if args.api_key:
        print_info(f"API Key: {args.api_key[:10]}...")
    
    tester = UDFServiceTester(args.host, args.api_key)
    results = {}
    
    # Run tests
    tests = {
        'health': ('Health Check', tester.test_health),
        'auth': ('Authentication', tester.test_auth),
        'parse': ('Parse UDF', lambda: tester.test_parse_udf(args.file)) if args.file else None,
        'ratelimit': ('Rate Limiting', tester.test_rate_limit),
        'filesize': ('File Size Limit', tester.test_file_size_limit),
    }
    
    print()
    for test_key, (test_name, test_func) in tests.items():
        if test_func is None:
            continue
        
        if args.test != 'all' and args.test != test_key:
            continue
        
        print(f"Testing: {test_name}...")
        try:
            success, message = test_func()
            results[test_key] = (success, message)
            
            if success:
                print_success(test_name)
            else:
                print_error(test_name)
            
            print(f"  {message[:200]}")
            print()
        except Exception as e:
            print_error(f"{test_name}: {e}")
            print()
    
    # Summary
    print(f"\n{Colors.BLUE}═══════════════════════════════════════════════════{Colors.END}")
    print("Summary:")
    passed = sum(1 for s, _ in results.values() if s)
    total = len(results)
    
    if passed == total:
        print_success(f"All tests passed ({passed}/{total})")
    else:
        print_warning(f"Some tests failed ({passed}/{total})")
    
    for test_key, (success, _) in results.items():
        status = f"{Colors.GREEN}PASS{Colors.END}" if success else f"{Colors.RED}FAIL{Colors.END}"
        print(f"  {test_key:15} {status}")
    
    print(f"{Colors.BLUE}═══════════════════════════════════════════════════{Colors.END}\n")
    
    sys.exit(0 if passed == total else 1)

if __name__ == '__main__':
    main()
