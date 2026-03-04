#!/usr/bin/env python3
"""Extract key fields from a UDF (content.xml CDATA) into JSON.

Usage: python tools/udf_extract_to_json.py uploads/ornek-1.udf outputs/ornek-1.json
"""
import sys
import zipfile
import re
import json
import unicodedata

def decode_cdata_bytes(raw_bytes):
    # backward-compatible wrapper for the richer meta function
    meta = decode_cdata_bytes_with_meta(raw_bytes)
    return meta['text']


def decode_cdata_bytes_with_meta(raw_bytes):
    """Decode CDATA inner bytes and return metadata including chosen encoding and scores.

    Returns dict: {text, encoding, score, candidates:[{enc,score,turkish_count,replace_count}]}
    """
    m = re.search(b'<!\[CDATA\[(.*?)\]\]>', raw_bytes, flags=re.DOTALL)
    if not m:
        inner = raw_bytes
    else:
        inner = m.group(1)

    candidates = ['utf-8', 'cp1254', 'iso-8859-9', 'latin1']
    best_text = None
    best_score = -10**9
    chosen_enc = None
    turkish_letters = set('çğıöşüÇĞİÖŞÜ')
    cand_list = []
    for enc in candidates:
        try:
            s = inner.decode(enc)
        except Exception:
            continue
        rep = s.count('\ufffd') + s.count('�')
        tur = sum(s.count(ch) for ch in turkish_letters)
        score = tur - rep * 5
        cand_list.append({'enc': enc, 'score': score, 'turkish': tur, 'replace': rep})
        if score > best_score:
            best_score = score
            best_text = s
            chosen_enc = enc

    if best_text is None:
        best_text = inner.decode('utf-8', errors='replace')
        chosen_enc = 'utf-8(replace)'
        best_score = -999

    # normalize
    try:
        best_text = unicodedata.normalize('NFC', best_text)
    except Exception:
        pass

    return {
        'text': best_text,
        'encoding': chosen_enc,
        'score': best_score,
        'candidates': cand_list
    }


def extract_fields(text):
    # normalize line endings and collapse multiple spaces but keep tabs to detect columns
    lines = [ln.rstrip('\r') for ln in text.splitlines()]
    joined = '\n'.join(lines)

    out = {}

    warnings = []
    confidences = {}

    def find_single_with_confidence(patterns, default=''):
        candidates = []
        for p in patterns:
            for m in re.finditer(p, joined, flags=re.IGNORECASE):
                val = m.group(1).strip()
                # simple heuristic confidence: length + turkish letters - replacement chars
                tur = sum(val.count(ch) for ch in 'çğıöşüÇĞİÖŞÜ')
                rep = val.count('\ufffd') + val.count('�')
                score = 0.4 + min(0.5, len(val) / 60.0) + min(0.2, tur * 0.1) - rep * 0.2
                score = max(0.0, min(1.0, score))
                candidates.append((val, score))
        if not candidates:
            return default, 0.0, []
        # sort by score desc
        candidates.sort(key=lambda x: x[1], reverse=True)
        vals = [c[0] for c in candidates]
        best, best_score = candidates[0]
        return best, best_score, vals

    out['basvuru_numarasi'], confidences['basvuru_numarasi'], _ = find_single_with_confidence([
        r'BA\s*ŞVURU\s*NUMARASI\s*[:\t]*\s*([0-9A-Za-z/\-]+)',
        r'BA\s*ŞVURU\s*NO\s*[:\t]*\s*([0-9A-Za-z/\-]+)',
        r'BA[SŞ]VURU\s*NUMARASI\s*[:\t]*\s*([^\n\r]+)'
    ])

    out['basvuru_tarihi'], confidences['basvuru_tarihi'], _ = find_single_with_confidence([
        r'BA\s*ŞVURU\s*TAR[Iİ]H[Iİ]\s*[:\t]*\s*([^\n\r]+)',
        r'BA[SŞ]VURU\s*TAR[Iİ]H[Iİ]\s*[:\t]*\s*([^\n\r]+)'
    ])

    # Extract ALL applicants from "BAŞVURU SAHİBİ BİLGİLERİ" or "BAŞVURUCU" blocks
    # B? makes the leading B optional to handle typos like "AŞVURU SAHİBİ"
    basvuru_sahipleri = []
    basvuru_sahipleri_blocks = re.split(
        r'B?A[SŞ]VURU\s+SAH[İI]B[İI]\s+B[İI]LG[İI]LER[İI]|(?:\d+\.\s*)?BA[SŞ]VURUCU\b',
        joined, flags=re.IGNORECASE
    )
    
    # Process each applicant block (skip first part which is before any heading)
    for block_idx, block in enumerate(basvuru_sahipleri_blocks[1:], 1):
        # Limit to content before next major section
        block = re.split(
            r'KAR[ŞS]I\s+TARAF|D[İI]ĞER\s+TARAF|BA[SŞ]VURU\s+B[İI]LG[İI]LER[İI]|BA[SŞ]VURU\s+SAH[İI]B[İI]',
            block, flags=re.IGNORECASE
        )[0]
        
        applicant = {}
        
        # Detect entity type marker (-Kişi İçin / -Kurum için)
        if re.search(r'-\s*Kurum\s+[iİ][çc][iİ]n', block, flags=re.IGNORECASE):
            applicant['entity_marker'] = 'kurum'
        elif re.search(r'-\s*K[iİ][şs][iİ]\s+[İIi][çc][iİ]n', block, flags=re.IGNORECASE):
            applicant['entity_marker'] = 'kisi'
        
        # Extract TC/Vergi No (supports T.C. Kimlik No, Vergi/Mersis/Detsis No, Mersis/VKN)
        m_tc = re.search(
            r'(?:T\.?C\.?\s*Kimlik\s*No|Vergi(?:/Mersis/Detsis)?\s*No|Mersis(?:/VKN)?\s*No|Mersis/VKN)\s*:\s*(?:VKN\s*:\s*)?([0-9]{5,25})',
            block, flags=re.IGNORECASE
        )
        applicant['kimlik_no'] = m_tc.group(1).strip() if m_tc else ''
        
        # Extract name: try Kurum Adı first (company), then Adı Soyadı / Adı-Soyadı
        # Use [ \t]* after colon (not \s*) to prevent matching across newlines
        m_kurum = re.search(r'Kurum\s+Ad[ıiIİ]\s*:[ \t]*([^\n\r]+)', block, flags=re.IGNORECASE)
        m_name = re.search(r'T?Ad[ıiIİ][\s\-]*Soyad[ıiIİ]\s*:[ \t]*([^\n\r]+)', block, flags=re.IGNORECASE)
        if m_kurum and m_kurum.group(1).strip():
            name_val = m_kurum.group(1).strip()
            # Check for multi-line company name (continuation on next indented line)
            pos = m_kurum.end()
            for cline in block[pos:].split('\n')[1:]:
                cstripped = cline.strip()
                if not cstripped:
                    break
                leading = len(cline) - len(cline.lstrip())
                if leading >= 10 and not re.match(r'\w[\w\s]*\s*:', cstripped):
                    name_val = name_val.rstrip() + ' ' + cstripped
                else:
                    break
            applicant['adi_soyadi'] = name_val
        elif m_name:
            applicant['adi_soyadi'] = m_name.group(1).strip()
        else:
            applicant['adi_soyadi'] = ''

        # ── Extract embedded TC / V.N. from name parentheses ──
        # e.g. "Yiğit YALMAN (TC: 20654252006 )" → name="Yiğit YALMAN", kimlik_no="20654252006"
        _name = applicant['adi_soyadi']
        m_tc_in_name = re.search(r'\(\s*(?:TC|T\.?C\.?)\s*[:\s]\s*([0-9]{9,11})\s*\)', _name)
        if m_tc_in_name:
            if not applicant['kimlik_no']:
                applicant['kimlik_no'] = m_tc_in_name.group(1).strip()
            applicant['adi_soyadi'] = re.sub(r'\s*\(\s*(?:TC|T\.?C\.?)\s*[:\s]\s*[0-9]{9,11}\s*\)', '', _name).strip()
        # e.g. "Şok Marketler A.Ş. (V.N. 8140131899)" → name cleaned, vergi_no extracted
        m_vn_in_name = re.search(r'\(\s*V\.?N\.?\s*[:\s]?\s*([0-9]{5,25})\s*\)', _name)
        if m_vn_in_name:
            if not applicant.get('vergi_no'):
                applicant['vergi_no'] = m_vn_in_name.group(1).strip()
            applicant['adi_soyadi'] = re.sub(r'\s*\(\s*V\.?N\.?\s*[:\s]?\s*[0-9]{5,25}\s*\)', '', applicant['adi_soyadi']).strip()

        # Extract address: try specific patterns first, then generic
        # Use [ \t]* after colon to prevent matching across newlines
        _app_addr_patterns = [
            r'Adres\s+ve\s+Cep\s*\(Zorunlu\)\s*:[ \t]*([^\n\r]+)',
            r'Adres\s+ve\s+Cep\s+Telefonu\s*:[ \t]*([^\n\r]+)',
            r'Adres\s+ve\s+Cep\s*:[ \t]*([^\n\r]+)',
            r'Mersis\s+[Aa]dresi\s+ve\s+Cep\s*:[ \t]*([^\n\r]+)',
            r'Adresi\s*:[ \t]*([^\n\r]+)',
            r'Adres\s*[:\t]+[ \t]*([^\n\r]+)',
        ]
        m_addr = None
        for _ap in _app_addr_patterns:
            m_addr = re.search(_ap, block, flags=re.IGNORECASE)
            if m_addr:
                break
        applicant['adres'] = m_addr.group(1).strip() if m_addr else ''
        
        # Extract vekil if present (handles VEKİLİ with Turkish İ)
        m_vek = re.search(r'(?:Ba[şs]vuran\s+)?Vek[iİ]l[iİ]?\s*:[ \t]*([^\n\r]+)', block, flags=re.IGNORECASE)
        applicant['vekil'] = m_vek.group(1).strip() if m_vek else ''
        
        # Extract Baro Sicil Numarası / No
        m_baro = re.search(r'Baro\s+Sicil\s*(?:Numaras[ıiIİ]|No)\s*:[ \t]*([^\n\r]+)', block, flags=re.IGNORECASE)
        applicant['baro_sicil'] = m_baro.group(1).strip() if m_baro else ''
        
        # Extract phone: Cep Telefonu(Zorunlu) / Cep Telefonu / İletişim / Telefon
        # Line-anchored (^) to avoid matching 'Cep Telefonu' inside 'Adres ve Cep Telefonu'
        _phone_patterns = [
            r'^\s*Cep\s+Telefonu\s*\(Zorunlu\)\s*:\s*([^\n\r]+)',
            r'^\s*Cep\s+Telefonu\s*:\s*([^\n\r]+)',
            r'^\s*[İIi]leti[şs]im\s*\(Cep[- ]Zorunlu\)\s*:\s*([^\n\r]+)',
            r'^\s*Telefon\s*:\s*([^\n\r]+)',
        ]
        m_phone = None
        for _pp in _phone_patterns:
            m_phone = re.search(_pp, block, flags=re.IGNORECASE | re.MULTILINE)
            if m_phone:
                break
        if m_phone:
            applicant['telefon_raw'] = m_phone.group(1).strip()
        
        # Extract email (e-posta / Mail: / standalone email line)
        m_email = re.search(r'e-?\s*posta\s*:\s*([^\n\r]+)', block, flags=re.IGNORECASE)
        if not m_email:
            # Try "Mail:" label (sometimes appended to phone line)
            m_email = re.search(r'\bMail\s*:\s*([\w.+-]+@[\w.-]+\.\w{2,}[^\n\r]*)', block, flags=re.IGNORECASE)
        if not m_email:
            # Try standalone email on its own line (e.g. vekil email)
            m_email = re.search(r'^\s*([\w.+-]+@[\w.-]+\.\w{2,})\s*$', block, flags=re.MULTILINE)
        if m_email:
            applicant['email'] = m_email.group(1).strip()
        
        # Skip empty entries
        if applicant['adi_soyadi'] or applicant['kimlik_no']:
            basvuru_sahipleri.append(applicant)
    
    out['basvuru_sahipleri'] = basvuru_sahipleri
    
    # For backwards compatibility, also set single fields from first applicant
    if basvuru_sahipleri:
        first = basvuru_sahipleri[0]
        out['tc_kimlik_no'] = first.get('kimlik_no', '')
        out['adi_soyadi'] = first.get('adi_soyadi', '')
        out['adres'] = first.get('adres', '')
        confidences['tc_kimlik_no'] = 1.0 if first.get('kimlik_no') else 0.0
        confidences['adi_soyadi'] = 1.0 if first.get('adi_soyadi') else 0.0
        confidences['adres'] = 1.0 if first.get('adres') else 0.0
    else:
        out['tc_kimlik_no'] = ''
        out['adi_soyadi'] = ''
        out['adres'] = ''
        confidences['tc_kimlik_no'] = 0.0
        confidences['adi_soyadi'] = 0.0
        confidences['adres'] = 0.0

    # ========== NUMBER AND ENTITY TYPE DETECTION HELPERS ==========
    
    def validate_tc(tc_raw):
        """Validate Turkish TC Kimlik No (11 digits with checksum)."""
        d = re.sub(r'\D', '', tc_raw or '')
        if len(d) != 11:
            return False
        if d[0] == '0':
            return False
        nums = [int(x) for x in d]
        odd_sum = sum(nums[i] for i in range(0, 9, 2))
        even_sum = sum(nums[i] for i in range(1, 8, 2))
        digit10 = ((odd_sum * 7) - even_sum) % 10
        digit11 = sum(nums[:10]) % 10
        return nums[9] == digit10 and nums[10] == digit11
    
    def classify_number(raw_number):
        """
        Classify a number string into its type.
        Returns dict with: type, value, pretty, compact
        Types: 'tc_kimlik', 'vergi_no', 'cep_tel', 'sabit_tel', 'unknown'
        """
        if not raw_number:
            return None
        
        digits = re.sub(r'\D', '', str(raw_number))
        if not digits:
            return None
        
        result = {'raw': raw_number, 'digits': digits}
        
        # TC Kimlik No: 11 haneli, algoritma geçerli
        if len(digits) == 11 and validate_tc(digits):
            result['type'] = 'tc_kimlik'
            result['pretty'] = digits
            result['compact'] = digits
            return result
        
        # Vergi No: 10 haneli (şirketler için)
        if len(digits) == 10 and not digits.startswith('05'):
            # Vergi numarası 10 hane ve 05 ile başlamaz
            result['type'] = 'vergi_no'
            result['pretty'] = digits
            result['compact'] = digits
            return result
        
        # Telefon numarası tespiti
        if len(digits) >= 10 and len(digits) <= 11:
            # Başına 0 ekle eğer 10 haneliyse
            if len(digits) == 10:
                digits_full = '0' + digits
            else:
                digits_full = digits
            
            area_code = digits_full[1:4]  # 0XXX -> XXX
            
            # Cep telefonu: 05XX ile başlar
            if digits_full.startswith('05'):
                result['type'] = 'cep_tel'
                result['pretty'] = f"0{digits_full[1:4]} {digits_full[4:7]} {digits_full[7:]}"
                result['compact'] = digits_full
                return result
            
            # Sabit telefon: 02XX, 03XX, 04XX ile başlar
            if area_code.startswith(('2', '3', '4')):
                result['type'] = 'sabit_tel'
                result['pretty'] = f"0{area_code} {digits_full[4:7]} {digits_full[7:]}"
                result['compact'] = digits_full
                return result
        
        # Bilinmeyen format
        result['type'] = 'unknown'
        result['pretty'] = digits
        result['compact'] = digits
        return result
    
    def is_company_name(name):
        """
        Detect if a name is a company (tüzel kişi) or real person (gerçek kişi).
        Returns: 'tuzel' for company, 'gercek' for person, 'unknown' if unclear
        """
        if not name:
            return 'unknown'
        
        name_upper = name.upper()
        
        # Şirket belirteçleri
        company_indicators = [
            r'\bLTD\b', r'\bLİMİTED\b', r'\bŞTİ\b', r'\bŞİRKETİ\b',
            r'\bA\.?Ş\.?\b', r'\bANONİM\b',
            r'\bSAN\b', r'\bSANAYİ\b', r'\bSAN\.\b',
            r'\bTİC\b', r'\bTİCARET\b', r'\bTİC\.\b',
            r'\bİNŞ\b', r'\bİNŞAAT\b', r'\bİNŞ\.\b',
            r'\bİTH\b', r'\bİTHALAT\b', r'\bİTH\.\b',
            r'\bİHR\b', r'\bİHRACAT\b', r'\bİHR\.\b',
            r'\bPAZ\b', r'\bPAZARLAMA\b',
            r'\bÜRETİM\b', r'\bHİZMET\b',
            r'\bGRUP\b', r'\bHOLDİNG\b',
            r'\bKOOP\b', r'\bKOOPERATİF\b',
            r'\bVAKFI\b', r'\bDERNEĞİ\b',
            r'\bMÜDÜRLÜĞÜ\b', r'\bBAŞKANLIĞI\b', r'\bDAİRESİ\b',  # Kamu kurumları
            r'\bBELEDİYESİ\b', r'\bVALİLİĞİ\b', r'\bKAYMAKAMLIĞI\b',
        ]
        
        for pattern in company_indicators:
            if re.search(pattern, name_upper):
                return 'tuzel'
        
        # Gerçek kişi tespiti: 2-4 kelime, Türkçe isim formatı
        words = name.split()
        if 2 <= len(words) <= 4:
            # Tüm kelimeler harf içeriyor mu?
            all_letters = all(re.match(r'^[A-ZÇĞİÖŞÜa-zçğıöşü]+$', w) for w in words)
            if all_letters:
                return 'gercek'
        
        return 'unknown'
    
    def normalize_phone(s):
        """Normalize phone number to standard format."""
        if not s:
            return ''
        classified = classify_number(s)
        if classified and classified['type'] in ('cep_tel', 'sabit_tel'):
            return {'compact': classified['compact'], 'pretty': classified['pretty'], 'type': classified['type']}
        return ''


    def clean_name_field(s):
        """Clean a person/name-like field by removing placeholder phones and trailing numeric artefacts.

        This helps remove page numbers, stray indices or OCR digits that appear after a name.
        """
        if not s:
            return s
        # remove placeholder phone fragments like (TEL: 000)
        s = re.sub(r'\s*\(\s*TEL\s*:\s*0+\s*\)', '', s, flags=re.IGNORECASE).strip()
        # remove any TEL fragments whether parenthesis closed or not, e.g. '(TEL: 05324' or 'TEL: 05324)'
        s = re.sub(r'(?i)\s*\(?\s*TEL\s*[:\-\s]*[0-9+\-\s\(\)\/\.]*\)?', '', s).strip()
        # remove common page markers like 'Sayfa 1' or 'Page 1'
        s = re.sub(r'(?i)\b(sayfa|page)\b[:\s]*\d+\b', '', s).strip()
        # remove trailing bare numbers or simple fractions (e.g. ' 1', ' (1)', ' 1/3')
        s = re.sub(r'[\s,;:/-]*\(?\d{1,6}(?:/\d{1,6})?\)?\s*$', '', s).strip()
        # remove trailing No: 123 patterns
        s = re.sub(r'No[:\s]*\d+$', '', s, flags=re.IGNORECASE).strip()
        # strip stray punctuation
        s = s.strip(' ,;')
        return s

    # Clean address: extract and classify numbers properly
    if out.get('adres'):
        addr = out['adres']
        # Extract any number that looks like phone/vergi from address
        phone_matches = re.findall(r'(?:Cep\s*Tel|CepTel|Telefon|Tel)\s*[:\s\-]*\(?([0-9+\-\s\(\)\/\.]{6,})\)?', addr, re.IGNORECASE)
        for pm in phone_matches:
            classified = classify_number(pm)
            if classified:
                if classified['type'] == 'cep_tel' and not out.get('__extracted_cep_tel'):
                    out['__extracted_cep_tel'] = classified
                elif classified['type'] == 'sabit_tel' and not out.get('__extracted_sabit_tel'):
                    out['__extracted_sabit_tel'] = classified
                elif classified['type'] == 'vergi_no' and not out.get('__extracted_vergi_no'):
                    out['__extracted_vergi_no'] = classified
        
        # Remove phone/tel fragments from address
        addr = re.sub(r'(?i)\b(?:Cep\s*Tel|CepTel|Telefon|Tel)\b[:\s\-]*\(?[0-9+\-\s\(\)\/\.]{6,}\)?', '', addr)
        addr = re.sub(r'[\t ]{2,}', ' ', addr).strip()
        
        # If address ends with a naked number, classify and extract
        m_trail = re.search(r'\(?\s*([0-9]{7,12})\s*\)?\s*$', addr)
        if m_trail:
            classified = classify_number(m_trail.group(1))
            if classified and classified['type'] != 'tc_kimlik':
                addr = re.sub(r'\s*\(?[0-9]{7,12}\)?\s*$', '', addr).strip()
                if classified['type'] == 'cep_tel' and not out.get('__extracted_cep_tel'):
                    out['__extracted_cep_tel'] = classified
                elif classified['type'] == 'sabit_tel' and not out.get('__extracted_sabit_tel'):
                    out['__extracted_sabit_tel'] = classified
                elif classified['type'] == 'vergi_no' and not out.get('__extracted_vergi_no'):
                    out['__extracted_vergi_no'] = classified
        out['adres'] = addr
    
    # Detect if applicant is company or person
    out['kisi_turu'] = is_company_name(out.get('adi_soyadi', ''))
    
    # Reclassify TC/Vergi based on entity type
    if out.get('tc_kimlik_no'):
        classified = classify_number(out['tc_kimlik_no'])
        if classified:
            if out['kisi_turu'] == 'tuzel' and classified['type'] == 'vergi_no':
                out['vergi_no'] = classified['compact']
                out['tc_kimlik_no'] = ''  # Şirketin TC'si olmaz
            elif out['kisi_turu'] == 'tuzel' and classified['type'] == 'tc_kimlik':
                # 11 haneli ama şirket - muhtemelen şirket sahibinin TC'si
                out['sahip_tc'] = classified['compact']
                out['tc_kimlik_no'] = ''
            # else keep as tc_kimlik_no for gercek kisi
    
    # Process each applicant: clean address, detect entity type, classify numbers
    for idx, applicant in enumerate(out.get('basvuru_sahipleri', [])):
        # Clean address
        addr = applicant.get('adres', '')
        if addr:
            # Extract phone numbers from address
            phone_matches = re.findall(r'(?:Cep\s*Tel|CepTel|Telefon|Tel)\s*[:\s\-]*\(?([0-9+\-\s\(\)\/\.]{6,})\)?', addr, re.IGNORECASE)
            for pm in phone_matches:
                classified = classify_number(pm)
                if classified and classified['type'] in ('cep_tel', 'sabit_tel'):
                    if not applicant.get('telefon'):
                        applicant['telefon'] = classified
            
            addr = re.sub(r'(?i)\b(?:Cep\s*Tel|CepTel|Telefon|Tel)\b[:\s\-]*\(?[0-9+\-\s\(\)\/\.]{6,}\)?', '', addr)
            addr = re.sub(r'[\t ]{2,}', ' ', addr).strip()
            
            # Trailing naked number
            m_trail = re.search(r'\(?\s*([0-9]{7,12})\s*\)?\s*$', addr)
            if m_trail:
                classified = classify_number(m_trail.group(1))
                if classified and classified['type'] != 'tc_kimlik':
                    addr = re.sub(r'\s*\(?[0-9]{7,12}\)?\s*$', '', addr).strip()
                    if classified['type'] in ('cep_tel', 'sabit_tel') and not applicant.get('telefon'):
                        applicant['telefon'] = classified
            
            applicant['adres'] = addr
        
        # Clean placeholder phones from name like "(TEL: 000)"
        name = applicant.get('adi_soyadi', '')
        if name:
            # Remove (TEL: 000) or (TEL: 0) placeholder patterns
            name = re.sub(r'\s*\(\s*TEL\s*:\s*0+\s*\)', '', name, flags=re.IGNORECASE).strip()
            applicant['adi_soyadi'] = name
        
        # Clean placeholder phones from vekil
        vekil = applicant.get('vekil', '')
        if vekil:
            # Use centralized cleaner to remove placeholder phones and trailing numbers
            applicant['vekil'] = clean_name_field(vekil)
        
        # Detect entity type
        applicant['kisi_turu'] = is_company_name(applicant.get('adi_soyadi', ''))
        
        # Classify kimlik number
        kimlik = applicant.get('kimlik_no', '')
        if kimlik:
            classified = classify_number(kimlik)
            if classified:
                if applicant['kisi_turu'] == 'tuzel':
                    if classified['type'] == 'vergi_no':
                        applicant['vergi_no'] = classified['compact']
                        applicant['tc_kimlik_no'] = ''
                    elif classified['type'] == 'tc_kimlik':
                        applicant['sahip_tc'] = classified['compact']
                        applicant['tc_kimlik_no'] = ''
                else:
                    if classified['type'] == 'tc_kimlik':
                        applicant['tc_kimlik_no'] = classified['compact']
                    elif classified['type'] == 'vergi_no':
                        applicant['vergi_no'] = classified['compact']
        
        out['basvuru_sahipleri'][idx] = applicant
    
    out['vekil'], confidences['vekil'], _ = find_single_with_confidence([
        r'Vek[iİ]l[iİ]?(?![a-zA-Z\u00C0-\u024FçğıöşüÇĞİÖŞÜ])\s*:\s*([^\n\r]+)',
        r'Vek[iİ]l[iİ](?![a-zA-Z\u00C0-\u024FçğıöşüÇĞİÖŞÜ])\s*[:\t]+\s*([^\n\r]+)'
    ])
    # Clean extracted single 'vekil' field from trailing numbers or page markers
    out['vekil'] = clean_name_field(out.get('vekil', ''))

    # Multiple counterpart sections: find blocks that start with KARŞI TARAF or DİĞER TARAF
    counterparts = []
    # Split by heading occurrences - use line-start anchor (MULTILINE) to avoid
    # matching "karşı taraf" embedded in free-text paragraphs
    parts = re.split(
        r'^\s*(?:KAR[ŞS]I\s+TARAF|D[İI]ĞER\s+TARAF)',
        joined, flags=re.IGNORECASE | re.MULTILINE
    )
    # first part is before first heading; subsequent parts represent entries
    for part in parts[1:]:
        # Limit counterpart block to content before next major section
        part_block = re.split(
            r'BA[SŞ]VURU\s+B[İI]LG[İI]LER[İI]|BA[SŞ]VURU\s+SAH[İI]B[İI]|(?:\d+\.\s*)?BA[SŞ]VURUCU\b',
            part, flags=re.IGNORECASE
        )[0]
        
        # Detect entity type marker
        ct_entity_marker = None
        if re.search(r'-\s*Kurum\s+[iİ][çc][iİ]n', part_block, flags=re.IGNORECASE):
            ct_entity_marker = 'kurum'
        elif re.search(r'-\s*K[iİ][şs][iİ]\s+[İIi][çc][iİ]n', part_block, flags=re.IGNORECASE):
            ct_entity_marker = 'kisi'
        
        # Extract name: try Kurum Adı first, then Adı Soyadı / Adı-Soyadı
        # Use [ \t]* after colon to prevent matching across newlines
        m_kurum_ct = re.search(r'Kurum\s+Ad[ıiIİ]\s*:[ \t]*([^\n\r]+)', part_block, flags=re.IGNORECASE)
        mname = re.search(r'T?Ad[ıiIİ][\s\-]*Soyad[ıiIİ]\s*:[ \t]*([^\n\r]+)', part_block, flags=re.IGNORECASE)
        
        # Extract address: try specific patterns first
        # Use [ \t]* after colon to prevent matching across newlines
        _ct_addr_patterns = [
            r'Adres\s+ve\s+Cep\s*\(Zorunlu\)\s*:[ \t]*([^\n\r]+)',
            r'Adres\s+ve\s+Cep\s+Telefonu\s*:[ \t]*([^\n\r]+)',
            r'Adres\s+ve\s+Cep\s*:[ \t]*([^\n\r]+)',
            r'Mersis\s+[Aa]dresi\s+ve\s+Cep\s*:[ \t]*([^\n\r]+)',
            r'Adresi\s*:[ \t]*([^\n\r]+)',
            r'Adres\s*[:\t]+[ \t]*([^\n\r]+)',
        ]
        maddr = None
        for _cap in _ct_addr_patterns:
            maddr = re.search(_cap, part_block, flags=re.IGNORECASE)
            if maddr:
                break
        
        # Extract TC/Vergi/Mersis number (supports Mersis/VKN format, up to 25 digits)
        mtc = re.search(
            r'(?:T\.?C\.?\s*Kimlik\s*No|Vergi(?:/Mersis/Detsis)?\s*No|Mersis(?:/VKN)?\s*No|Mersis/VKN)\s*:\s*(?:VKN\s*:\s*)?([0-9]{5,25})',
            part_block, flags=re.IGNORECASE
        )
        
        # Also try 'Karşı Taraf :' as inline name field (some formats use it)
        m_inline_ct = re.search(r'Kar[şS][ıiIİ]\s+Taraf\s*[:\t]+[ \t]*([^\n\r]+)', part_block, flags=re.IGNORECASE)
        
        entry = {}
        # Set name from Kurum Adı, Adı Soyadı, or 'Karşı Taraf :' inline
        if m_kurum_ct and m_kurum_ct.group(1).strip():
            name_val_ct = m_kurum_ct.group(1).strip()
            # Check for multi-line company name continuation
            pos = m_kurum_ct.end()
            for cline in part_block[pos:].split('\n')[1:]:
                cstripped = cline.strip()
                if not cstripped:
                    break
                leading = len(cline) - len(cline.lstrip())
                if leading >= 10 and not re.match(r'\w[\w\s]*\s*:', cstripped):
                    name_val_ct = name_val_ct.rstrip() + ' ' + cstripped
                else:
                    break
            entry['adi_soyadi'] = name_val_ct
        elif mname:
            entry['adi_soyadi'] = mname.group(1).strip()
        elif m_inline_ct:
            # Strip leading colon from inline Karşı Taraf: name
            ct_name = m_inline_ct.group(1).strip().lstrip(':').strip()
            entry['adi_soyadi'] = ct_name
        else:
            entry['adi_soyadi'] = ''
        entry['adres'] = maddr.group(1).strip() if maddr else ''
        
        # Set entity marker if detected
        if ct_entity_marker:
            entry['entity_marker'] = ct_entity_marker
        
        # if empty, try to extract first non-empty line as name
        if not entry['adi_soyadi']:
            for ln in part.splitlines():
                ln2 = ln.strip()
                if not ln2:
                    continue
                # Skip heading fragments and entity markers
                if re.match(r'^(?:B[İI]LG[İI]LER[İI]|-\s*K[iİ][şs][iİ]\s+[İIi][çc][iİ]n|-\s*Kurum\s+[iİ][çc][iİ]n)\s*$', ln2, flags=re.IGNORECASE):
                    continue
                entry['adi_soyadi'] = ln2
                break
        
        # Extract counterpart vekil if present
        m_ct_vek = re.search(r'(?:Ba[şs]vuran\s+)?Vek[iİ]l[iİ]?\s*:[ \t]*([^\n\r]+)', part_block, flags=re.IGNORECASE)
        if m_ct_vek:
            entry['vekil'] = clean_name_field(m_ct_vek.group(1).strip())
        
        # Extract counterpart Baro Sicil
        m_ct_baro = re.search(r'Baro\s+Sicil\s*(?:Numaras[ıiIİ]|No)\s*:[ \t]*([^\n\r]+)', part_block, flags=re.IGNORECASE)
        if m_ct_baro:
            entry['baro_sicil'] = m_ct_baro.group(1).strip()
        
        # Extract counterpart phone (line-start anchored)
        _ct_phone_patterns = [
            r'^\s*Cep\s+Telefonu\s*\(Zorunlu\)\s*:\s*([^\n\r]+)',
            r'^\s*Cep\s+Telefonu\s*:\s*([^\n\r]+)',
            r'^\s*[İIi]leti[şs]im\s*\(Cep[- ]Zorunlu\)\s*:\s*([^\n\r]+)',
            r'^\s*Telefon\s*:\s*([^\n\r]+)',
        ]
        m_ct_phone = None
        for _cpp in _ct_phone_patterns:
            m_ct_phone = re.search(_cpp, part_block, flags=re.IGNORECASE | re.MULTILINE)
            if m_ct_phone:
                break
        if m_ct_phone:
            entry['telefon_raw'] = m_ct_phone.group(1).strip()
        
        # Extract counterpart email
        m_ct_email = re.search(r'e-?\s*posta\s*:\s*([^\n\r]+)', part_block, flags=re.IGNORECASE)
        if not m_ct_email:
            m_ct_email = re.search(r'\bMail\s*:\s*([\w.+-]+@[\w.-]+\.\w{2,}[^\n\r]*)', part_block, flags=re.IGNORECASE)
        if not m_ct_email:
            m_ct_email = re.search(r'^\s*([\w.+-]+@[\w.-]+\.\w{2,})\s*$', part_block, flags=re.MULTILINE)
        if m_ct_email:
            entry['email'] = m_ct_email.group(1).strip()

        # Clean placeholder phones from name like "(TEL: 000)"
        name = entry.get('adi_soyadi', '')
        if name:
            name = re.sub(r'\s*\(\s*TEL\s*:\s*0+\s*\)', '', name, flags=re.IGNORECASE).strip()
            entry['adi_soyadi'] = name
        
        # Detect entity type (company or person)
        entry['kisi_turu'] = is_company_name(entry.get('adi_soyadi', ''))
        
        # Process TC/Vergi number based on entity type
        if mtc:
            num_classified = classify_number(mtc.group(1))
            if num_classified:
                if entry['kisi_turu'] == 'tuzel':
                    # Şirket - vergi no olmalı
                    if num_classified['type'] == 'vergi_no':
                        entry['vergi_no'] = num_classified['compact']
                    elif num_classified['type'] == 'tc_kimlik':
                        entry['sahip_tc'] = num_classified['compact']  # Şirket sahibi TC
                else:
                    # Gerçek kişi - TC olmalı
                    if num_classified['type'] == 'tc_kimlik':
                        entry['tc_kimlik_no'] = num_classified['compact']
                    elif num_classified['type'] == 'vergi_no':
                        entry['vergi_no'] = num_classified['compact']  # Belki şahıs şirketi
        
        # Skip entries that are actually footer/meta lines
        adi = (entry.get('adi_soyadi') or '').strip()
        adres_field = (entry.get('adres') or '')
        
        # Comprehensive footer/meta detection patterns
        footer_patterns = [
            # "Bilgi Sahibi Mi : HAYIR" vb.
            r'Bilgi\s*Sahibi',
            r'^Bilgi\b',
            # "Ayrıntılı Bilgi İçin : ..." footer
            r'Ayr[ıi]nt[ıi]l[ıi]\s+Bilgi',
            # "NOT :" or "NOT:" sections
            r'^NOT\s*:',
            r'\bNOT\s*:',
            # "EKİ :" attachment lines
            r'^EK[İI]\s*:',
            # Separator lines (underscores, dashes)
            r'^[_\-=]{5,}',
            # Staff/clerk titles that appear in footer
            r'ZAB[IİI]T\s*K[ÂA]T[İI]B[İI]',
            r'M[ÜU]D[ÜU]R[ÜU]',
            # "Adres :" footer (single space before colon, not tabs)
            r'^Adres\s:',
            # Generic footer markers
            r'Ba[şs]vuru\s+Konusu',
            r'Dava\s+T[üu]r[üu]',
            r'Dosya\s+T[üu]r[üu]',
            r'Uyu[şs]mazl[ıi]k\s+T[üu]r[üu]',
            r'M[üu]racaat\s+Durumu',
            # Heading fragments that can appear as fallback names
            r'^B[İI]LG[İI]LER[İI]\s*$',
            # Entity type markers
            r'^-\s*K[iİ][şs][iİ]\s+[İIi][çc][iİ]n\s*$',
            r'^-\s*Kurum\s+[iİ][çc][iİ]n\s*$',
            # Very short content that's likely a label
            r'^[A-Z]{1,3}\s*:',  # Like "T.C." or "NO:"
        ]
        
        # Check if name or address matches any footer pattern
        is_footer = False
        for pattern in footer_patterns:
            if re.search(pattern, adi, flags=re.IGNORECASE):
                is_footer = True
                break
            if re.search(pattern, adres_field, flags=re.IGNORECASE):
                is_footer = True
                break
        
        if is_footer:
            continue
        
        # Skip if name is too short (likely a label or artifact)
        if len(adi) < 3:
            continue
        
        # Skip if name is too long (likely a paragraph from free-text description)
        if len(adi) > 200:
            continue
        
        # Skip if name looks like a sentence (starts lowercase, contains verbs/clauses)
        if adi and adi[0].islower() and len(adi) > 50:
            continue
        
        # Skip if name looks like a form field label (ends with colon or contains only caps + colon)
        if re.match(r'^[A-ZÇĞİÖŞÜ\s]+\s*:.*$', adi) and len(adi.split(':')[0].strip()) < 20:
            continue

        # ── Extract embedded TC / V.N. from counterpart name parentheses ──
        ct_name = entry.get('adi_soyadi', '')
        m_ct_tc = re.search(r'\(\s*(?:TC|T\.?C\.?)\s*[:\s]\s*([0-9]{9,11})\s*\)', ct_name)
        if m_ct_tc:
            if not entry.get('tc_kimlik_no'):
                entry['tc_kimlik_no'] = m_ct_tc.group(1).strip()
            entry['adi_soyadi'] = re.sub(r'\s*\(\s*(?:TC|T\.?C\.?)\s*[:\s]\s*[0-9]{9,11}\s*\)', '', ct_name).strip()
        m_ct_vn = re.search(r'\(\s*V\.?N\.?\s*[:\s]?\s*([0-9]{5,25})\s*\)', entry.get('adi_soyadi', ''))
        if m_ct_vn:
            if not entry.get('vergi_no'):
                entry['vergi_no'] = m_ct_vn.group(1).strip()
            entry['adi_soyadi'] = re.sub(r'\s*\(\s*V\.?N\.?\s*[:\s]?\s*[0-9]{5,25}\s*\)', '', entry['adi_soyadi']).strip()
        # Clean leading colon/space artifacts (e.g. ": Şok Marketler")
        if entry.get('adi_soyadi', '').startswith(':'):
            entry['adi_soyadi'] = entry['adi_soyadi'].lstrip(': ').strip()

        counterparts.append(entry)

    out['karsi_taraflar'] = counterparts

    # Clean addresses inside counterparts: extract and classify numbers properly
    for idx, entry in enumerate(counterparts):
        a = entry.get('adres') or ''
        if a:
            # Extract labelled phone/tel numbers
            phone_matches = re.findall(r'(?:Cep\s*Tel|CepTel|Telefon|Tel)\s*[:\s\-]*\(?([0-9+\-\s\(\)\/\.]{6,})\)?', a, re.IGNORECASE)
            for pm in phone_matches:
                classified = classify_number(pm)
                if classified:
                    if classified['type'] == 'cep_tel' and not entry.get('cep_tel'):
                        entry['cep_tel'] = classified
                    elif classified['type'] == 'sabit_tel' and not entry.get('sabit_tel'):
                        entry['sabit_tel'] = classified
                    elif classified['type'] == 'vergi_no' and not entry.get('vergi_no'):
                        entry['vergi_no'] = classified['compact']
            
            # Remove phone fragments from address
            a = re.sub(r'(?i)\b(?:Cep\s*Tel|CepTel|Telefon|Tel)\b[:\s\-]*\(?[0-9+\-\s\(\)\/\.]{6,}\)?', '', a)
            a = re.sub(r'[\t ]{2,}', ' ', a).strip()
            
            # Trailing naked number - classify it
            m_tr = re.search(r'\(?\s*([0-9]{7,12})\s*\)?\s*$', a)
            if m_tr:
                classified = classify_number(m_tr.group(1))
                if classified and classified['type'] != 'tc_kimlik':
                    a = re.sub(r'\s*\(?[0-9]{7,12}\)?\s*$', '', a).strip()
                    if classified['type'] == 'cep_tel' and not entry.get('cep_tel'):
                        entry['cep_tel'] = classified
                    elif classified['type'] == 'sabit_tel' and not entry.get('sabit_tel'):
                        entry['sabit_tel'] = classified
                    elif classified['type'] == 'vergi_no' and not entry.get('vergi_no'):
                        entry['vergi_no'] = classified['compact']
            
            entry['adres'] = a
        
        # Set telefon field from best available
        if entry.get('cep_tel'):
            entry['telefon'] = entry['cep_tel']
        elif entry.get('sabit_tel'):
            entry['telefon'] = entry['sabit_tel']
        
        counterparts[idx] = entry

    # additional fields heuristically
    # Extract Dava Türü and Uyuşmazlık Türü as SEPARATE fields
    m_dava = re.search(r'Dava\s*T[üu]r[üu]\s*[:\t]*\s*([^\n\r]+)', joined, flags=re.IGNORECASE)
    if not m_dava:
        m_dava = re.search(r'Dosya\s*T[üu]r[üu]\s*[:\t]*\s*([^\n\r]+)', joined, flags=re.IGNORECASE)
    out['dava_turu'] = m_dava.group(1).strip().rstrip(',') if m_dava else ''
    confidences['dava_turu'] = 0.9 if m_dava else 0.0

    m_uyus = re.search(r'Uyu[şs]mazl[ıi]k\s*T[üu]r[üu]\s*[:\t]*\s*([^\n\r]+)', joined, flags=re.IGNORECASE)
    out['uyusmazlik_turu'] = m_uyus.group(1).strip() if m_uyus else ''

    # Extract Başvuru Konusu Müracaat Durumu
    basvuru_konusu = ''
    m_bk = re.search(r'Ba[sş]vuru\s+Konusu\s+M[üu]racaat\s+Durumu\s*[:\t]*\s*([^\n\r]+)', joined, flags=re.IGNORECASE)
    if m_bk:
        basvuru_konusu = m_bk.group(1).strip()
    out['basvuru_konusu_muracaat_durumu'] = basvuru_konusu

    # build warnings for low confidence or missing fields
    for k, v in confidences.items():
        if v < 0.5 and out.get(k):
            warnings.append(f"Low confidence for {k}: {v:.2f}")

    for k in ['adres', 'adi_soyadi', 'tc_kimlik_no']:
        if not out.get(k):
            warnings.append(f"Missing field {k}")

    # extract 'Bilgi Sahibi Mi' if present anywhere
    bsm = None
    m_bsm = re.search(r'Bilgi\s*Sahibi\s*Mi\s*[:\t]*\s*([^\n\r]+)', joined, flags=re.IGNORECASE)
    if not m_bsm:
        m_bsm = re.search(r'Kar[ŞS]i\s+Taraf\s+Bilgi\s+Sahibi\s+Mi\s*[:\t]*\s*([^\n\r]+)', joined, flags=re.IGNORECASE)
    if m_bsm:
        bsm = m_bsm.group(1).strip()
    out['bilgi_sahibi_mi'] = bsm or ''

    # ========== ARABULUCU BÜROSU ==========
    arabulucu_burosu = ''
    m_ab = re.search(r'(\S.*?Arabuluculuk\s+B[üu]rosu)', joined, re.IGNORECASE)
    if m_ab:
        arabulucu_burosu = m_ab.group(1).strip()
    out['arabulucu_burosu'] = arabulucu_burosu

    # ========== FOOTER AND EKI EXTRACTION ==========
    
    # Extract "Ayrıntılı Bilgi İçin" field (clerk name)
    ayrintili_bilgi = ''
    m_ayrintili = re.search(r'Ayr[ıi]nt[ıi]l[ıi]\s+Bilgi\s+[İIi][çc]in\s*[:\t]*\s*([^\n\r]+)', joined, flags=re.IGNORECASE)
    if m_ayrintili:
        ayrintili_bilgi = m_ayrintili.group(1).strip()
    out['ayrintili_bilgi_icin'] = ayrintili_bilgi
    
    # Extract "Adres" from footer (e.g., "İzmir Arabuluculuk Bürosu")
    # Footer format: "Adres : Xxx" (single space before colon)
    # Regular address format: "Adres\t\t:" (tabs before colon)
    footer_adres = ''
    m_fadres = re.search(r'Adres\s:\s([^\t\n]+)', joined)
    if m_fadres:
        footer_adres = m_fadres.group(1).strip()
    out['footer_adres'] = footer_adres

    # ========== BEYAN / EKİ / NOT EXTRACTION (raw text sections) ==========

    # Beyan (declaration) — "Başkaca bir usul ... talep ederim."
    beyan_metni = ''
    m_beyan = re.search(
        r'(Ba[şs]kaca\s+bir\s+usul.*?talep\s+ederim\.?)',
        joined, re.DOTALL | re.IGNORECASE,
    )
    if m_beyan:
        beyan_metni = re.sub(r'[ \t]+', ' ', m_beyan.group(1)).strip()
    out['beyan_metni'] = beyan_metni

    # EKİ — "EKİ : VEKALETNAME SURETİ, ..."
    eki_metni = ''
    m_eki = re.search(r'EK[İI]\s*:\s*([^\n\r]+)', joined, re.IGNORECASE)
    if m_eki:
        eki_metni = m_eki.group(1).strip()
    out['eki_metni'] = eki_metni

    # NOT (informational) — "NOT: Başvurudaki tarafların ... (HUAK Yönetmeliği m. 25/7)"
    not_bilgi_metni = ''
    m_not_bilgi = re.search(
        r'NOT\s*:?\s*(Ba[şs]vurudaki\s+taraf.*?(?:Y[öo]netmeli[ğg]i\s*m\.\s*25/7\)?|beyan\s+edilmesi\s+[öo]nemlidir\.))',
        joined, re.DOTALL | re.IGNORECASE,
    )
    if m_not_bilgi:
        not_bilgi_metni = re.sub(r'[ \t]+', ' ', m_not_bilgi.group(1)).strip()
    out['not_bilgi_metni'] = not_bilgi_metni

    # NOT (assignment) — "NOT : BAŞVURUCU/VEKİLİNİN TALEBİ İLE ..."
    not_atama_metni = ''
    # Match all NOT lines that are NOT the informational note
    m_not_atama = re.search(
        r'NOT\s*:\s*((?:BA[ŞS]VURUCU|VEK[İI]L).*?)(?:\n\s*Adres\s*:|_{3,}|$)',
        joined, re.DOTALL | re.IGNORECASE,
    )
    if m_not_atama:
        raw_not = m_not_atama.group(1).strip()
        # Collect all lines until separator
        not_atama_metni = re.sub(r'\s+', ' ', raw_not).strip()
    out['not_atama_metni'] = not_atama_metni

    # Extract signature names (Başvurucu line and subsequent name)
    # For multiple applicants, extract all names after "Başvurucu" labels
    imza_adi = ''
    imza_isimleri = []
    
    # Find the signature section between EKİ and NOT
    m_imza_section = re.search(r'EK[İI]\s*:.*?\n([\s\S]*?)NOT\s*:', joined, re.IGNORECASE)
    if m_imza_section:
        imza_block = m_imza_section.group(1)
        # Count "Başvurucu" labels
        basvurucu_count = len(re.findall(r'Ba[şs]vurucu', imza_block, re.IGNORECASE))
        
        # Extract names - they come after all the "Başvurucu" labels
        # Pattern: multiple "Başvurucu" then names then "Vekili"
        m_names = re.search(r'(?:Ba[şs]vurucu\s*\n\s*)+(.+?)(?:Vekili|$)', imza_block, re.IGNORECASE | re.DOTALL)
        if m_names:
            names_part = m_names.group(1).strip()
            # Split by newline and filter empty
            name_lines = [n.strip() for n in names_part.split('\n') if n.strip() and not re.match(r'^Ba[şs]vurucu$', n.strip(), re.IGNORECASE)]
            imza_isimleri = name_lines[:basvurucu_count] if basvurucu_count else name_lines
    
    # Fallback to old method if no names found
    if not imza_isimleri:
        m_basv_imza = re.search(r'Ba[şs]vurucu\s*\n([A-ZÇĞİÖŞÜa-zçğıöşü][^\n]+)', joined, re.IGNORECASE)
        if m_basv_imza:
            imza_isimleri = [m_basv_imza.group(1).strip()]
    
    out['imza_adi'] = imza_isimleri[0] if imza_isimleri else ''
    out['imza_isimleri'] = imza_isimleri  # All signature names for multiple applicants
    
    # Extract vekili imza (Vekili after signature names, before NOT)
    imza_vekili = ''
    # Try to find Vekili in signature section
    if m_imza_section:
        imza_block = m_imza_section.group(1)
        m_vek = re.search(r'Vekili\s*[:\t]*\s*([^\n]+)', imza_block, re.IGNORECASE)
        if m_vek:
            imza_vekili = m_vek.group(1).strip()
    
    # Fallback: search in whole document
    if not imza_vekili:
        m_vek_imza = re.search(r'EK[İI][\s\S]*?Vekili\s*[:\t]*\s*([^\n]+?)(?:\n|NOT)', joined, re.IGNORECASE)
        if m_vek_imza:
            imza_vekili = m_vek_imza.group(1).strip()
    # Clean signature vekili name from stray numbers
    imza_vekili = clean_name_field(imza_vekili)
    out['imza_vekili'] = imza_vekili

    # ========== VALIDATION AND PHONE ASSIGNMENT ==========
    
    validations = {}
    # normalize TC
    tc_raw = out.get('tc_kimlik_no') or ''
    tc_digits = re.sub(r'\D', '', tc_raw)
    out['tc_kimlik_no'] = tc_digits
    validations['tc_valid'] = validate_tc(tc_digits)
    if tc_digits and not validations['tc_valid']:
        # Maybe it's a vergi no for a company?
        if out.get('kisi_turu') == 'tuzel' and len(tc_digits) == 10:
            out['vergi_no'] = tc_digits
            out['tc_kimlik_no'] = ''
            warnings.append('TC alanındaki numara vergi no olarak tanındı (şirket)')
        else:
            warnings.append('TC kimlik numarası geçerli değil')

    # collect and classify all phone-like numbers found in text
    phones = re.findall(r'\d{7,12}', joined)
    classified_phones = []
    for ph in phones:
        classified = classify_number(ph)
        if classified:
            classified_phones.append(classified)
    out['telefonlar'] = classified_phones

    # Try to assign phones to specific fields by proximity heuristics
    # basvuru_telefonu: prefer extracted cep_tel, then sabit_tel
    basvuru_phone = None
    if out.get('__extracted_cep_tel'):
        basvuru_phone = out.pop('__extracted_cep_tel')
    elif out.get('__extracted_sabit_tel'):
        basvuru_phone = out.pop('__extracted_sabit_tel')
    else:
        # Look near applicant block
        m_app = re.search(r'BA[SŞ]VURU\s+SAHİBİ.*?Adres[\s\S]{0,200}', joined, flags=re.IGNORECASE)
        if m_app:
            chunk = m_app.group(0)
            phs = re.findall(r'\d{7,12}', chunk)
            for ph in phs[::-1]:
                classified = classify_number(ph)
                if classified and classified['type'] in ('cep_tel', 'sabit_tel'):
                    basvuru_phone = classified
                    break
    
    if not basvuru_phone and classified_phones:
        # fallback: first cep_tel or sabit_tel in document
        for cp in classified_phones:
            if cp['type'] in ('cep_tel', 'sabit_tel'):
                basvuru_phone = cp
                break
    
    out['basvuru_telefonu'] = basvuru_phone or None
    
    # Clean up extracted phone temps
    out.pop('__extracted_cep_tel', None)
    out.pop('__extracted_sabit_tel', None)
    out.pop('__extracted_vergi_no', None)

    # vekil_telefonu: search in 'Vekili' line(s)
    vekil_phone = None
    m_vek = re.search(r'Vekil(?:i)?\s*[:\t\s]*([^\n\r]+)', joined, flags=re.IGNORECASE)
    if m_vek:
        vek_chunk = m_vek.group(1)
        phs = re.findall(r'\d{7,12}', vek_chunk)
        for ph in phs[::-1]:
            classified = classify_number(ph)
            if classified and classified['type'] in ('cep_tel', 'sabit_tel'):
                vekil_phone = classified
                break
    out['vekil_telefonu'] = vekil_phone or None

    # Diğer taraf telefonları zaten yukarıda adres temizleme sırasında atandı

    # Post-process names: strip phone placeholders like '(TEL: 000)' from names and attach warnings
    def is_placeholder_phone(digits):
        if not digits:
            return True
        try:
            return int(digits) == 0
        except Exception:
            return False

    # clean main applicant name
    name = out.get('adi_soyadi') or ''
    if name:
        # look for TEL patterns in the name
        m_tel = re.search(r'\(?.*?TEL\s*[:\-\s]*([^\)\n\r]+)\)?', name, flags=re.IGNORECASE)
        if m_tel:
                tel_raw = m_tel.group(1)
                tel_digits = re.sub(r'\D', '', tel_raw)
                if is_placeholder_phone(tel_digits):
                    # remove the tel fragment from the name (parenthesized TEL or inline TEL: ...)
                    name = re.sub(r'\([^)]*TEL[^)]*\)', '', name, flags=re.IGNORECASE)
                    name = re.sub(r'TEL\s*[:\-\s]*\(?[^\)\n\r]+?\)?', '', name, flags=re.IGNORECASE).strip()
                    warnings.append('Placeholder phone removed from adi_soyadi')
                else:
                    # legitimate phone found in name: remove from name and prefer this for basvuru_telefonu if empty
                    name = re.sub(r'\([^)]*TEL[^)]*\)', '', name, flags=re.IGNORECASE)
                    name = re.sub(r'TEL\s*[:\-\s]*\(?[^\)\n\r]+?\)?', '', name, flags=re.IGNORECASE).strip()
                if not out.get('basvuru_telefonu'):
                    out['basvuru_telefonu'] = normalize_phone(tel_digits) or out.get('basvuru_telefonu')
        # also strip stray trailing commas/slashes
        name = name.strip(' ,;')
        out['adi_soyadi'] = name

    # clean counterpart names similarly and update their telefon field if phone present in name
    for idx, entry in enumerate(counterparts):
        ename = entry.get('adi_soyadi', '')
        if ename:
            m_tel = re.search(r'\(?.*?TEL\s*[:\-\s]*([^\)\n\r]+)\)?', ename, flags=re.IGNORECASE)
            if m_tel:
                tel_raw = m_tel.group(1)
                tel_digits = re.sub(r'\D', '', tel_raw)
                if is_placeholder_phone(tel_digits):
                    ename = re.sub(r'\([^)]*TEL[^)]*\)', '', ename, flags=re.IGNORECASE)
                    ename = re.sub(r'TEL\s*[:\-\s]*\(?[^\)\n\r]+?\)?', '', ename, flags=re.IGNORECASE).strip()
                    warnings.append(f'Placeholder phone removed from karsi_taraflar[{idx}].adi_soyadi')
                else:
                    ename = re.sub(r'\([^)]*TEL[^)]*\)', '', ename, flags=re.IGNORECASE)
                    ename = re.sub(r'TEL\s*[:\-\s]*\(?[^\)\n\r]+?\)?', '', ename, flags=re.IGNORECASE).strip()
                    # if telefon not already assigned, set it
                    if not entry.get('telefon'):
                        entry['telefon'] = normalize_phone(tel_digits) or None
            entry['adi_soyadi'] = ename
            counterparts[idx] = entry

    # TC correction suggestions
    tc_suggestions = []
    if tc_digits and not validations.get('tc_valid'):
        # try add leading zero
        if len(tc_digits) == 10:
            cand = '0' + tc_digits
            if validate_tc(cand):
                tc_suggestions.append(cand)
        # try remove leading zero if present
        if len(tc_digits) == 12 and tc_digits.startswith('90'):
            cand = tc_digits[2:]
            if validate_tc(cand):
                tc_suggestions.append(cand)
        # try common OCR fixes: replace O with 0, I with 1
        alt = tc_raw.replace('O', '0').replace('o', '0').replace('I', '1').replace('l', '1')
        alt_digits = re.sub(r'\D', '', alt)
        if alt_digits and alt_digits != tc_digits and validate_tc(alt_digits):
            tc_suggestions.append(alt_digits)

    validations['tc_suggestions'] = tc_suggestions

    return out, confidences, warnings, validations


def main(argv):
    if len(argv) < 2:
        print('Usage: udf_extract_to_json.py <input.udf> [output.json]')
        return 2
    inp = argv[1]
    outp = argv[2] if len(argv) > 2 else inp.replace('.udf', '.json').replace('.UDF', '.json')

    with zipfile.ZipFile(inp, 'r') as z:
        if 'content.xml' not in z.namelist():
            print('content.xml not found in', inp)
            return 3
        raw = z.read('content.xml')

    meta = decode_cdata_bytes_with_meta(raw)
    text = meta['text']
    fields, confidences, warnings, validations = extract_fields(text)

    result = {'fields': fields, 'confidences': confidences, 'warnings': warnings, 'validations': validations, 'metadata': meta}

    with open(outp, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print('Wrote', outp)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
