import urllib.request
import json
import base64
import os

# Konfigurasi Sonar
SONAR_URL = "http://10.150.10.61:9000"
PROJECT_KEY = "hive-prod"
TOKEN = "sqa_41f5251a3264187ae3fcb676dd573a780b045715"

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

def generate_markdown(data):
    issues = data.get("issues", [])
    if not issues:
        return "# Laporan SonarQube\n\n✅ Tidak ada issue yang ditemukan atau semua issue telah terselesaikan!"
    
    md_content = f"# Laporan Issue SonarQube (Total: {len(issues)})\n\n"
    md_content += "| Severity | Type | File | Line | Message |\n"
    md_content += "|---|---|---|---|---|\n"
    
    for issue in issues:
        severity = issue.get("severity", "UNKNOWN")
        type_ = issue.get("type", "UNKNOWN")
        component = issue.get("component", "").replace(f"{PROJECT_KEY}:", "")
        line = issue.get("line", "-")
        message = issue.get("message", "-")
        
        md_content += f"| {severity} | {type_} | `{component}` | {line} | {message} |\n"
        
    return md_content

if __name__ == "__main__":
    try:
        print("Mengambil data issue dari SonarQube...")
        data = fetch_issues()
        md_report = generate_markdown(data)
        
        # Simpan ke file markdown
        with open("SONAR_ISSUES.md", "w", encoding="utf-8") as f:
            f.write(md_report)
            
        print("Berhasil membuat SONAR_ISSUES.md")
    except Exception as e:
        print(f"Gagal mengambil data dari SonarQube: {e}")
