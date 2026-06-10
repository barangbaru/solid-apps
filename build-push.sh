#!/usr/bin/env bash
# build-push.sh — Build, tag, dan push image ke Nexus private registry
set -euo pipefail

# ── Konfigurasi ───────────────────────────────────────────────────────────────
REGISTRY="nexus.devops.mmi-pt.com"
IMAGE_NAME="evaluasi-kinerja"
IMAGE_TAG="${1:-latest}"           # Gunakan argumen pertama sebagai tag, atau 'latest'
NEXUS_USER="developer"
NEXUS_PASS="nop4ssword"

FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"
LATEST_IMAGE="${REGISTRY}/${IMAGE_NAME}:latest"

# ── Warna output ──────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
step()    { echo -e "\n${YELLOW}[$1/4]${NC} $2"; }

echo ""
echo "============================================================"
echo "  BUILD & PUSH: ${FULL_IMAGE}"
echo "============================================================"

# ── 1. Build ──────────────────────────────────────────────────────────────────
step 1 "Building Docker image..."
docker build -t "${FULL_IMAGE}" . || error "Docker build gagal!"
info "Build selesai."

# ── 2. Tag latest ─────────────────────────────────────────────────────────────
if [[ "${IMAGE_TAG}" != "latest" ]]; then
    step 2 "Menambah tag latest: ${LATEST_IMAGE}"
    docker tag "${FULL_IMAGE}" "${LATEST_IMAGE}"
    info "Tag latest ditambahkan."
else
    step 2 "Tag sudah latest, skip."
fi

# ── 3. Login ──────────────────────────────────────────────────────────────────
step 3 "Login ke ${REGISTRY}..."
echo "${NEXUS_PASS}" | docker login "${REGISTRY}" -u "${NEXUS_USER}" --password-stdin \
    || error "Login ke Nexus gagal! Cek kredensial atau koneksi."
info "Login berhasil."

# ── 4. Push ───────────────────────────────────────────────────────────────────
step 4 "Pushing ${FULL_IMAGE}..."
docker push "${FULL_IMAGE}" || error "Push ${FULL_IMAGE} gagal!"

if [[ "${IMAGE_TAG}" != "latest" ]]; then
    echo "  Pushing ${LATEST_IMAGE}..."
    docker push "${LATEST_IMAGE}" || warn "Push latest gagal (non-fatal)."
fi

echo ""
echo "============================================================"
info "SUKSES! Image tersedia di:"
echo "  ${FULL_IMAGE}"
echo "============================================================"
echo ""
echo "Untuk deploy, jalankan di server:"
echo "  docker-compose pull && docker-compose up -d"
echo ""
