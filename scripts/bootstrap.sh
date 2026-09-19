#!/usr/bin/env bash
# ============================================================================
# SDOC Hackathon 2026 · bootstrap —— 把官方两个 zip 解成可运行的本地布局
#
# 幂等：已解出的文件不会重复解压覆盖。
# 单数据集原则：不落第二份 inbox/attachments（两份字节同源，共 770 个文件）。
#
#   sdoc-hackathon-bundle.zip → data/                （Inbox("data") 直接可用）
#                              官方 package 解出后，data/ 也被 mount 为容器 /data
#   sdoc-hackathon-docker.zip → server/              （官方评分服务，逐字节不改）
#                             → docker-compose.yml   （只改 2 行 volume 路径）
#                             → eval/private/ground_truth.json（全仓库唯一一份，已 gitignore）
#                             → eval/generator/      （生成器，用于不同 seed 的 holdout）
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
echo "[bootstrap] root = $ROOT"

BUNDLE_ZIP="sdoc-hackathon-bundle.zip"
DOCKER_ZIP="sdoc-hackathon-docker.zip"

# 原始 zip 移入 official-zips/（已被 .gitignore 排除，内含答案键，绝不入库）
mkdir -p official-zips
for zip in "$BUNDLE_ZIP" "$DOCKER_ZIP"; do
  if [ -f "$zip" ]; then
    mv -f "$zip" "official-zips/$zip"
    echo "[bootstrap] moved $zip -> official-zips/"
  fi
done

BUNDLE_ZIP="official-zips/$BUNDLE_ZIP"
DOCKER_ZIP="official-zips/$DOCKER_ZIP"

# ---------------------------------------------------------------------------
# 1. 参赛数据包 → data/
# ---------------------------------------------------------------------------
mkdir -p data
if [ ! -f data/loader.py ] && [ -f "$BUNDLE_ZIP" ]; then
  echo "[bootstrap] 解压参赛数据包 -> data/"
  unzip -q -o "$BUNDLE_ZIP" -d data
else
  echo "[bootstrap] data/ 已就绪，跳过"
fi
touch data/.gitkeep

# ---------------------------------------------------------------------------
# 2. 官方评分服务 → server/ + 根 docker-compose.yml
# ---------------------------------------------------------------------------
if [ ! -f server/app.py ] && [ -f "$DOCKER_ZIP" ]; then
  echo "[bootstrap] 解压官方评分服务 -> server/"
  mkdir -p server
  unzip -q -o "$DOCKER_ZIP" 'server/*' -d .
  unzip -q -o "$DOCKER_ZIP" 'docker-compose.yml' -d .
else
  echo "[bootstrap] server/ 已就绪，跳过"
fi
touch server/.gitkeep

# ---------------------------------------------------------------------------
# 3. 答案键 → eval/private/（唯一一份，gitignore 已排除）
# ---------------------------------------------------------------------------
mkdir -p eval/private eval/generator eval/report
if [ ! -f eval/private/ground_truth.json ] && [ -f "$DOCKER_ZIP" ]; then
  echo "[bootstrap] 抽取答案键 -> eval/private/ground_truth.json（仅本地开发回归用）"
  unzip -q -p "$DOCKER_ZIP" 'data_v2/ground_truth.json' > eval/private/ground_truth.json
fi

# ---------------------------------------------------------------------------
# 4. 生成器（用于不同 seed 的 holdout 泛化测试）
# ---------------------------------------------------------------------------
if [ ! -f eval/generator/generate.py ] && [ -f "$DOCKER_ZIP" ]; then
  echo "[bootstrap] 抽取生成器 -> eval/generator/"
  for f in generate.py pools.py render.py shipment.py emails.py edgecases.py; do
    unzip -q -p "$DOCKER_ZIP" "data_v2/$f" > "eval/generator/$f"
  done
fi

# ---------------------------------------------------------------------------
# 5. 修正 docker-compose.yml 的挂载路径（唯一偏离官方原件的地方）
#    官方默认挂 ./data_v2 —— 我们只保留一份数据集 data/，避免双份 770 个文件
# ---------------------------------------------------------------------------
if [ -f docker-compose.yml ]; then
  if grep -q './data_v2' docker-compose.yml 2>/dev/null; then
    echo "[bootstrap] 修正 docker-compose.yml 的 volume 路径"
    sed -i.bak \
      -e 's#\./data_v2/ground_truth\.json:/secrets/ground_truth\.json:ro#./eval/private/ground_truth.json:/secrets/ground_truth.json:ro#' \
      -e 's#\./data_v2:/data:ro#./data:/data:ro#' \
      docker-compose.yml
    rm -f docker-compose.yml.bak
  fi
  echo "[bootstrap] docker-compose.yml volume 现状："
  grep -E '^\s+- \./' docker-compose.yml || true
fi

echo
echo "[bootstrap] 完成。下一步："
echo "  python3 scripts/verify_dataset.py     # 校验数据集完整性"
echo "  make run20                            # 20 封冒烟跑批"
