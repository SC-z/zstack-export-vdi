#!/usr/bin/env bash
set -euo pipefail

WORKDIR=`pwd`
DEFAULT_QEMU_IMG="$WORKDIR/bin/qemu-img"
DEFAULT_LIB_PATH="$WORKDIR/lib:/opt/qemu-lib"

usage() {
  cat <<'EOF'
用法: export_vdi.sh <uuid> <product_name>

示例:
  ./export_vdi.sh ad75dfb9a0544b2dac38774becc5a433 qdata-7.0.0

提示:
  - 在执行本脚本前，请已在虚拟机内部完成 fstrim / 等操作。
  - QEMU_IMG_BIN 环境变量可用于指定自定义 qemu-img，可选。
EOF
}

log() {
  printf '[%s] %s\n' "$(date '+%F %T')" "$*"
}

log_stage() {
  log "==== $* ===="
}

die() {
  log "ERROR: $*"
  exit 1
}

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || die "命令 '$cmd' 未找到，请先安装"
}

trap 'die "执行中断 (行: $LINENO)"' ERR

[[ $# -eq 2 ]] || { usage; exit 1; }
UUID="$1"
PRODUCT_NAME="$2"


QEMU_IMG_BIN="${QEMU_IMG_BIN:-$DEFAULT_QEMU_IMG}"
if [[ ! -x "$QEMU_IMG_BIN" ]]; then
  QEMU_IMG_BIN="$(command -v qemu-img 2>/dev/null || true)"
fi
[[ -n "$QEMU_IMG_BIN" ]] || die "未找到 qemu-img，可通过环境变量 QEMU_IMG_BIN 指定"
[[ -x "$QEMU_IMG_BIN" ]] || die "qemu-img 未设置可执行权限: $QEMU_IMG_BIN"

require_cmd virsh

OUTPUT_QCOW2="$WORKDIR/${PRODUCT_NAME}.qcow2"
OUTPUT_VDI="$WORKDIR/${PRODUCT_NAME}.vdi"

[[ ! -e "$OUTPUT_QCOW2" ]] || die "目标文件已存在: $OUTPUT_QCOW2"
[[ ! -e "$OUTPUT_VDI" ]] || die "目标文件已存在: $OUTPUT_VDI"

log_stage "环境准备"
log "使用工作目录: $WORKDIR"
log "使用 qemu-img: $QEMU_IMG_BIN"
cd "$WORKDIR"

log_stage "确认虚拟机状态"
domain_state=$(virsh domstate "$UUID" 2>/dev/null | tr -d '\r' | tr '[:upper:]' '[:lower:]' || true)
[[ -n "$domain_state" ]] || die "无法获取虚拟机状态，请确认 UUID 是否正确"
log "当前状态: $domain_state"

log_stage "关闭虚拟机"
if [[ "$domain_state" == "running" ]]; then
  virsh destroy "$UUID"
  log "已执行 virsh destroy"
else
  log "虚拟机未运行，无需关闭"
fi

log_stage "定位源 qcow2"
disk_path=$(virsh domblklist ad75dfb9a0544b2dac38774becc5a433 --details  |  grep qcow2 |awk '{print $4}')
[[ -n "$disk_path" ]] || die "未找到磁盘路径，请检查虚拟机配置"
[[ -f "$disk_path" ]] || die "磁盘文件不存在: $disk_path"
log "源镜像: $disk_path"

log_stage "导出完整 qcow2"
if [[ -n "$DEFAULT_LIB_PATH" ]]; then
  LD_LIBRARY_PATH="$DEFAULT_LIB_PATH${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    "$QEMU_IMG_BIN" convert "$disk_path" -O qcow2 "$OUTPUT_QCOW2"
else
    "$QEMU_IMG_BIN" convert "$disk_path" -O qcow2 "$OUTPUT_QCOW2"
fi
log "qcow2 输出: $OUTPUT_QCOW2"

log_stage "转换 qcow2 -> vdi"
if [[ -n "$DEFAULT_LIB_PATH" ]]; then
  LD_LIBRARY_PATH="$DEFAULT_LIB_PATH${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    "$QEMU_IMG_BIN" convert -f qcow2 -O vdi "$OUTPUT_QCOW2" "$OUTPUT_VDI"
else
  "$QEMU_IMG_BIN" convert -f qcow2 -O vdi "$OUTPUT_QCOW2" "$OUTPUT_VDI"
fi
log "vdi 输出: $OUTPUT_VDI"

# log_stage "生成 vdi MD5"
# md5sum "$OUTPUT_VDI" | tee "${OUTPUT_VDI}.md5"

log_stage "流程完成"
log "请手动验证 vdi，并在测试结束后取消 MD5 相关注释"
