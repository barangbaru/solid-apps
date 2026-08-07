import urllib.request
import json
import base64
import os

# Konfigurasi Sonar
SONAR_URL = "http://10.150.10.61:9000"
PROJECT_KEY = "hive-prod"
TOKEN = os.environ.get("SONAR_TOKEN", "")
REPORT_HEADER = "# Riwayat Laporan Issue SonarQube\n\n"

# Endpoint API SonarQube untuk mengambil issue (Open & Unresolved)
API_ENDPOINT = f"{SONAR_URL}/api/issues/search?componentKeys={PROJECT_KEY}&statuses=OPEN,CONFIRMED,REOPENED&resolved=false"

def fetch_issues():
    # Encode token untuk Basic Auth
    auth_str = f"{TOKEN}:"
    b64_auth_str = base64.b64encode(auth_str.encode()).decode()
    
    req = urllib.request.Request(API_ENDPOINT)
    req.add_header("Authorization", f"Basic {b64_auth_str}")
    
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read().decode())

import datetime

def generate_markdown(data):
    issues = data.get("issues", [])
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    md_content = f"## Scan Date: {now}\n\n"
    
    if not issues:
        md_content += "✅ Tidak ada issue yang ditemukan atau semua issue telah terselesaikan!\n\n---\n\n"
        return md_content
    
    md_content += f"**Total Issues:** {len(issues)}\n\n"
    md_content += "| Severity | Type | File | Line | Message |\n"
    md_content += "|---|---|---|---|---|\n"
    
    for issue in issues:
        severity = issue.get("severity", "UNKNOWN")
        type_ = issue.get("type", "UNKNOWN")
        component = issue.get("component", "").replace(f"{PROJECT_KEY}:", "")
        line = issue.get("line", "-")
        message = issue.get("message", "-")
        
        md_content += f"| {severity} | {type_} | `{component}` | {line} | {message} |\n"
        
    md_content += "\n---\n\n"
    return md_content

if __name__ == "__main__":
    try:
        if not TOKEN:
            print("Peringatan: SONAR_TOKEN belum diatur sebagai environment variable.")
            
        print("Mengambil data issue dari SonarQube...")
        data = fetch_issues()
        new_report = generate_markdown(data)
        
        # Simpan ke file markdown dengan tracking (menyisipkan di paling atas)
        file_name = "SONAR_ISSUES.md"
        existing_content = ""
        
        if os.path.exists(file_name):
            with open(file_name, "r", encoding="utf-8") as f:
                existing_content = f.read()
            
            # Buang judul utama jika sudah ada agar tidak ganda saat digabungkan
            if existing_content.startswith(REPORT_HEADER):
                existing_content = existing_content.replace(REPORT_HEADER, "", 1)
        
        final_content = REPORT_HEADER + new_report + existing_content
        
        with open(file_name, "w", encoding="utf-8") as f:
            f.write(final_content)
            
        print(f"Berhasil memperbarui {file_name}")
    except Exception as e:
        print(f"Gagal mengambil data dari SonarQube: {e}")
