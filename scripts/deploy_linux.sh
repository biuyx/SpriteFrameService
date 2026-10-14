#!/usr/bin/env bash
# 一键部署到 Linux 服务器：本机构建前端、打包当前提交，传到服务器上替换，
# 按需重启服务并确认它真的起来了；起不来自动回滚。
#
# 用法（仓库根目录下；Windows 用 Git Bash，或双击 scripts/deploy_linux.bat）：
#   scripts/deploy_linux.sh               部署当前提交（HEAD）
#   scripts/deploy_linux.sh --dry-run     只看会改哪些文件、要不要重启，不动服务器
#   scripts/deploy_linux.sh --status      服务器上是哪个版本、比本地落后几个提交
#   scripts/deploy_linux.sh --rollback    回滚到上一次部署前的版本
# 选项：
#   --allow-dirty   带着未提交的改动部署（默认拒绝：服务器上跑的应该对得上某个提交）
#   --restart       不管改了什么都重启；--no-restart 改了后端也不重启
#   --force         有任务在跑也照样重启（默认拒绝：重启会打断正在生成、抠图的任务）
#
# 目标服务器写在 scripts/deploy.env（不入库，照 deploy.env.example 复制一份），
# 同名环境变量优先。部署机要能免密 SSH 登录服务器，服务器用户要能免密 sudo。
#
# 只同步代码与前端产物：backend/app、backend/run.py、依赖清单、rtmlib、scripts、
# frontend/dist、README / CHANGELOG。不碰 backend/.env、数据目录、模型、tools、.venv。
# 依赖清单变了只提醒、不自动装：GPU 版 onnxruntime 是钉死的组合，盲目 pip install
# 可能把它换掉（见 backend/requirements-gpu-pascal.txt）。
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"

# ---------------------------------------------------------------- 参数与配置
MODE=deploy; ALLOW_DIRTY=no; RESTART=auto; FORCE=no
for a in "$@"; do
  case "$a" in
    --dry-run) MODE=dryrun ;;
    --status) MODE=status ;;
    --rollback) MODE=rollback ;;
    --allow-dirty) ALLOW_DIRTY=yes ;;
    --restart) RESTART=always ;;
    --no-restart) RESTART=never ;;
    --force) FORCE=yes ;;
    -h|--help) awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
    *) echo "不认识的参数：$a（--help 看用法）"; exit 2 ;;
  esac
done

for v in DEPLOY_HOST DEPLOY_DIR DEPLOY_SERVICE DEPLOY_PORT DEPLOY_HEALTH_TIMEOUT; do
  eval "_env_$v=\${$v-}"
done
if [ -f scripts/deploy.env ]; then
  # 记事本存的文件带 CRLF，\r 混进主机名会让 ssh 报一个看不懂的错
  . <(tr -d '\r' < scripts/deploy.env)
fi
for v in DEPLOY_HOST DEPLOY_DIR DEPLOY_SERVICE DEPLOY_PORT DEPLOY_HEALTH_TIMEOUT; do
  eval "[ -n \"\$_env_$v\" ] && $v=\$_env_$v || true"
done
if [ -z "${DEPLOY_HOST:-}" ]; then
  echo "未配置目标服务器：复制 scripts/deploy.env.example 为 scripts/deploy.env，填上 DEPLOY_HOST"
  exit 2
fi
DEPLOY_DIR=${DEPLOY_DIR:-/opt/sprite}
DEPLOY_SERVICE=${DEPLOY_SERVICE:-spriteframe}
DEPLOY_PORT=${DEPLOY_PORT:-8000}
DEPLOY_HEALTH_TIMEOUT=${DEPLOY_HEALTH_TIMEOUT:-180}   # 重启后最多等几秒（启动要先加载抠图模型）

# 部署的源码（前端另外构建后放 frontend/dist）。Windows 专用的批处理不往服务器上放
SOURCES=(backend/app backend/run.py backend/requirements.txt backend/requirements-gpu-pascal.txt
         rtmlib scripts CHANGELOG.md README.md
         ':(exclude)scripts/*.bat' ':(exclude)scripts/*.ps1')

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# ---------------------------------------------------------------- 服务器端脚本
# 每次随命令一起传上去执行（服务器上不用预先装任何东西）。参数：
#   目录 服务名 端口 健康检查最长等待秒数 动作(status|dryrun|apply|rollback) 重启(auto|always|never)
#   强制(yes|no) [包的 sha256]
read -r -d '' REMOTE_SCRIPT <<'REMOTE' || true
set -uo pipefail
DIR=$1; SERVICE=$2; PORT=$3; WAIT=$4; ACTION=$5; RESTART=${6:-auto}; FORCE=${7:-no}; SHA=${8:-}
D="$DIR/.deploy"; IN="$D/incoming"; BK="$D/backups"
MIRROR_DIRS="backend/app rtmlib frontend/dist"   # 整目录同步，多出来的旧文件删掉
MERGE_DIRS="scripts"                             # 只增改不删：服务器上可能放了自己的脚本
FILES="backend/run.py backend/requirements.txt backend/requirements-gpu-pascal.txt CHANGELOG.md README.md DEPLOYED"
echo "[远端] 开始"

die() { echo "[远端] ✗ $*"; exit 1; }
cleanup() { rm -rf "$IN" "$D/incoming.tar.gz" "$D/restore"; }

# 列出 $IN 相对线上的改动：A 新增 / M 修改 / D 删除；只差行尾的不算改动
changes() {
  python3 - "$DIR" "$IN" "$MIRROR_DIRS" "$MERGE_DIRS" "$FILES" <<'PY'
import hashlib, os, sys
cur, new = sys.argv[1], sys.argv[2]
mirror, merge, files = sys.argv[3].split(), sys.argv[4].split(), sys.argv[5].split()
files = [f for f in files if f != "DEPLOYED"]

def walk(base, root):
    out = set()
    for dp, dns, fns in os.walk(os.path.join(base, root)):
        dns[:] = [d for d in dns if d != "__pycache__"]
        out.update(os.path.relpath(os.path.join(dp, f), base) for f in fns)
    return out

def digest(p):
    with open(p, "rb") as f:
        return hashlib.sha1(f.read().replace(b"\r\n", b"\n")).hexdigest()

for root in mirror + merge + files:
    if root in files:
        a = {root} if os.path.isfile(os.path.join(cur, root)) else set()
        b = {root} if os.path.isfile(os.path.join(new, root)) else set()
    else:
        a, b = walk(cur, root), walk(new, root)
    for f in sorted(a | b):
        if f not in b:
            if root in mirror:
                print("D", f)
        elif f not in a:
            print("A", f)
        elif digest(os.path.join(cur, f)) != digest(os.path.join(new, f)):
            print("M", f)
PY
}

# 正在跑或排队的任务：第一行是个数（查不到时是 ?），后面几行是明细
running_jobs() {
  local hdr body token code
  hdr=$(mktemp); body=$(mktemp); chmod 600 "$hdr"
  token=$(grep -E '^SPRITE_AUTH_TOKEN=' "$DIR/backend/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d "\"'\r")
  [ -n "$token" ] && printf 'Authorization: Bearer %s\n' "$token" > "$hdr"
  code=$(curl -s -m 5 -o "$body" -w '%{http_code}' -H "@$hdr" "http://127.0.0.1:$PORT/api/jobs?limit=200" </dev/null)
  rm -f "$hdr"
  case "$code" in
    200) python3 -c '
import json, sys
act = [j for j in json.load(sys.stdin).get("jobs", []) if j.get("status") in ("queued", "running")]
print(len(act))
for j in act[:8]:
    print("    %s  %s  %d%%  %s" % (j.get("type"), j.get("status"),
                                   int(j.get("progress") or 0), (j.get("message") or "")[:40]))' \
           < "$body" || echo "?" ;;
    000) echo 0 ;;                      # 服务没在跑，自然也没有任务
    *) echo "?"; echo "    查任务列表返回 HTTP $code" ;;
  esac
  rm -f "$body"
}

# 有任务在跑就不重启——除非 --force。查不到任务列表（比如口令不对）也不重启：宁可不部署，
# 不能把正在生成的任务当成「没有」给打断了
guard_jobs() {
  [ "$FORCE" = yes ] && return 0
  local jobs n
  jobs=$(running_jobs); n=$(printf '%s\n' "$jobs" | head -1)
  if [ "$n" = "?" ]; then
    printf '%s\n' "$jobs" | tail -n +2
    cleanup
    die "确认不了有没有任务在跑，没有重启（服务器上什么都没改）。确定没人在用就加 --force"
  fi
  if [ "${n:-0}" -gt 0 ] 2>/dev/null; then
    echo "[远端] ✗ 有 $n 个任务在跑或排队，重启会把它们打断："
    printf '%s\n' "$jobs" | tail -n +2
    cleanup
    die "等任务跑完再部署，或加 --force 强制（服务器上什么都没改）"
  fi
}

# 把某个目录里的部署内容同步到线上
sync_from() {
  local src=$1 p
  for p in $MIRROR_DIRS; do
    [ -d "$src/$p" ] || continue
    mkdir -p "$DIR/$p"
    rsync -a --delete --exclude=__pycache__ "$src/$p/" "$DIR/$p/" || return 1
  done
  for p in $MERGE_DIRS; do
    [ -d "$src/$p" ] || continue
    mkdir -p "$DIR/$p"
    rsync -a "$src/$p/" "$DIR/$p/" || return 1
  done
  for p in $FILES; do
    [ -f "$src/$p" ] || continue
    mkdir -p "$(dirname "$DIR/$p")"
    cp "$src/$p" "$DIR/$p" || return 1
  done
}

# 等服务真正可用：启动时要先加载抠图模型，端口要过一阵才开（P4 上首次约一分钟）
health() {
  local t0; t0=$(date +%s)
  while [ $(( $(date +%s) - t0 )) -lt "$WAIT" ]; do
    if curl -sf -m 3 "http://127.0.0.1:$PORT/api/health" -o /dev/null </dev/null; then
      echo "[远端] 服务已就绪（重启后 $(( $(date +%s) - t0 )) 秒）"
      return 0
    fi
    sleep 2
  done
  return 1
}

restart_and_check() {
  echo "[远端] 重启 $SERVICE（加载模型期间端口要等一会儿才开，最长等 $WAIT 秒）…"
  sudo -n systemctl restart "$SERVICE" </dev/null || return 1
  sleep 2
  if ! health; then
    echo "[远端] 服务 $WAIT 秒内没有就绪，最近的日志："
    sudo -n journalctl -u "$SERVICE" -n 30 --no-pager </dev/null | sed 's/^/    /'
    return 1
  fi
}

# 解开备份并同步回线上（备份里没有部署记录，说明那时还没用本脚本部署过）
restore() {
  rm -rf "$D/restore"; mkdir -p "$D/restore"
  tar -xzf "$1" -C "$D/restore" || return 1
  sync_from "$D/restore" || return 1
  [ -f "$D/restore/DEPLOYED" ] || rm -f "$DIR/DEPLOYED"
  rm -rf "$D/restore"
}

# 比较 $IN 与线上，打印改动，设置 NEED（要不要重启）
plan() {
  CH=$(changes) || die "比较文件失败"
  N=$(printf '%s\n' "$CH" | grep -c . || true)
  echo "[远端] 有变化的文件：$N 个"
  printf '%s\n' "$CH" | grep . | head -40 | sed 's/^/    /'
  [ "$N" -gt 40 ] && echo "    …（共 $N 个）"
  NEED=no
  printf '%s\n' "$CH" | grep -qE '^[AMD] (backend/app/|backend/run\.py|rtmlib/)' && NEED=yes
  case "$RESTART" in always) NEED=yes ;; never) NEED=no ;; esac
  if printf '%s\n' "$CH" | grep -qE '^[AMD] backend/requirements'; then
    echo "[远端] ⚠ 依赖清单有变化，本脚本不自动装依赖。请登录服务器按 README 手动装"
    echo "         （GPU 版 onnxruntime 是钉死的组合，直接 pip install -r 可能把它换成 CPU 版）"
  fi
  echo "[远端] 需要重启服务：$([ "$NEED" = yes ] && echo 是 || echo 否)"
}

case "$ACTION" in
status)
  if [ -f "$DIR/DEPLOYED" ]; then cat "$DIR/DEPLOYED"; else echo "commit="; fi
  echo "service=$(systemctl is-active "$SERVICE" 2>/dev/null)"
  echo "backups=$(ls -1 "$BK"/*.tar.gz 2>/dev/null | wc -l)"
  ;;

dryrun|apply)
  [ -f "$D/incoming.tar.gz" ] || die "没收到部署包"
  if [ -n "$SHA" ]; then
    echo "$SHA  $D/incoming.tar.gz" | sha256sum -c --status || { cleanup; die "部署包校验不通过（传输不完整？），请重试"; }
  fi
  rm -rf "$IN"; mkdir -p "$IN"
  tar -xzf "$D/incoming.tar.gz" -C "$IN" || { cleanup; die "解包失败"; }
  plan
  if [ "$ACTION" = dryrun ]; then cleanup; echo "[远端] 试运行，服务器上什么都没改"; exit 0; fi
  if [ "$N" -eq 0 ] && [ "$NEED" = no ]; then
    cp "$IN/DEPLOYED" "$DIR/DEPLOYED"; cleanup
    echo "[远端] ✓ 代码没有变化，只更新了部署记录"
    exit 0
  fi
  if [ "$NEED" = yes ]; then
    sudo -n true </dev/null 2>&1 || { cleanup; die "重启服务需要免密 sudo"; }
    guard_jobs
  fi

  TS=$(date +%Y%m%d-%H%M%S)
  mkdir -p "$BK"
  EXIST=""
  for p in $MIRROR_DIRS $MERGE_DIRS $FILES; do [ -e "$DIR/$p" ] && EXIST="$EXIST $p"; done
  # shellcheck disable=SC2086
  tar -czf "$BK/$TS.tar.gz" -C "$DIR" --exclude=__pycache__ $EXIST || { cleanup; die "备份失败，什么都没改"; }
  ls -1t "$BK"/*.tar.gz | tail -n +6 | xargs -r rm -f      # 只留最近 5 份
  echo "[远端] 已备份部署前的版本：$BK/$TS.tar.gz"

  if ! sync_from "$IN"; then
    echo "[远端] ✗ 同步文件出错，恢复部署前的版本 …"
    restore "$BK/$TS.tar.gz"; rm -f "$BK/$TS.tar.gz"; cleanup
    die "已恢复，服务器保持原样"
  fi
  cleanup

  if [ "$NEED" = yes ] && ! restart_and_check; then
    echo "[远端] ✗ 新版本没能正常启动，自动回滚到部署前的版本 …"
    restore "$BK/$TS.tar.gz" || die "回滚失败！请登录服务器处理：备份在 $BK/$TS.tar.gz"
    rm -f "$BK/$TS.tar.gz"
    if restart_and_check; then
      die "已回滚，服务恢复正常。上面是新版本启动失败时的日志"
    fi
    die "回滚后服务仍起不来，请登录服务器排查：sudo journalctl -u $SERVICE -n 100"
  fi
  echo "[远端] ✓ 部署完成"
  ;;

rollback)
  LAST=$(ls -1t "$BK"/*.tar.gz 2>/dev/null | head -1)
  [ -n "$LAST" ] || die "没有可回滚的备份"
  echo "[远端] 回滚到 $(basename "$LAST" .tar.gz) 那次部署之前的版本"
  rm -rf "$IN"; mkdir -p "$IN"
  tar -xzf "$LAST" -C "$IN" || die "备份解不开：$LAST"
  plan
  if [ "$NEED" = yes ]; then
    sudo -n true </dev/null 2>&1 || { cleanup; die "重启服务需要免密 sudo"; }
    guard_jobs
  fi
  sync_from "$IN" || { cleanup; die "同步文件出错，请登录服务器检查"; }
  [ -f "$IN/DEPLOYED" ] || rm -f "$DIR/DEPLOYED"
  cleanup
  rm -f "$LAST"                       # 用掉的备份删掉，再回滚就退到更早一版
  if [ "$NEED" = yes ]; then
    restart_and_check || die "回滚后服务没起来，请登录服务器排查：sudo journalctl -u $SERVICE -n 100"
  fi
  echo "[远端] ✓ 已回滚"
  ;;

*) die "未知动作：$ACTION" ;;
esac
REMOTE

# ---------------------------------------------------------------- 与服务器通信
# 本机到服务器的 SSH 偶尔在握手阶段就被断开（代理 / 网络抖动）。只有「连接都没建立」
# 才重试——远端脚本已经开始跑了再重来，可能把一件事做两遍
remote() {
  local log rc t
  log="$TMP/remote.log"
  for t in 1 2 3 4 5; do
    set +e
    printf '%s\n' "$REMOTE_SCRIPT" | ssh "${SSH_OPTS[@]}" "$DEPLOY_HOST" \
      "mkdir -p '$DEPLOY_DIR/.deploy' && cat > '$DEPLOY_DIR/.deploy/remote.sh' && bash '$DEPLOY_DIR/.deploy/remote.sh' '$DEPLOY_DIR' '$DEPLOY_SERVICE' '$DEPLOY_PORT' '$DEPLOY_HEALTH_TIMEOUT' $*" \
      2>&1 | tee "$log" | grep -v '^\[远端\] 开始$'
    rc=${PIPESTATUS[1]}
    set -e
    if [ "$rc" -ne 255 ] || grep -q '^\[远端\] 开始' "$log"; then
      return "$rc"
    fi
    echo "  （连接没建立，第 $t/5 次，3 秒后重试）"
    sleep 3
  done
  return 255
}

upload() {
  local t rc
  for t in 1 2 3 4 5; do
    set +e
    ssh "${SSH_OPTS[@]}" "$DEPLOY_HOST" \
      "mkdir -p '$DEPLOY_DIR/.deploy' && cat > '$DEPLOY_DIR/.deploy/incoming.tar.gz'" < "$1"
    rc=$?
    set -e
    [ "$rc" -eq 0 ] && return 0
    [ "$rc" -ne 255 ] && return "$rc"
    echo "  （连接没建立，第 $t/5 次，3 秒后重试）"
    sleep 3
  done
  return 255
}

# ---------------------------------------------------------------- 各模式
if [ "$MODE" = status ]; then
  echo "==> 查询 $DEPLOY_HOST:$DEPLOY_DIR"
  out=$(remote status) || { echo "$out"; exit 1; }
  dep=$(printf '%s\n' "$out" | sed -n 's/^commit=//p')
  get() { printf '%s\n' "$out" | sed -n "s/^$1=//p"; }
  echo "    服务状态：$(get service)    可回滚的备份：$(get backups) 份"
  if [ -z "$dep" ]; then
    echo "    服务器上没有部署记录（还没用本脚本部署过）"
    exit 0
  fi
  echo "    线上版本：${dep:0:7} $(get subject)"
  echo "    部署于 $(get time)，由 $(get by)$([ "$(get dirty)" = yes ] && echo '，带未提交的改动')"
  head=$(git rev-parse HEAD)
  if [ "$dep" = "$head" ]; then
    echo "    ✓ 与本地当前提交一致"
  elif git cat-file -e "$dep^{commit}" 2>/dev/null; then
    behind=$(git rev-list --count "$dep..$head")
    ahead=$(git rev-list --count "$head..$dep")
    [ "$behind" -gt 0 ] && { echo "    线上比本地落后 $behind 个提交："; git log --oneline "$dep..$head" | head -20 | sed 's/^/      /'; }
    [ "$ahead" -gt 0 ] && echo "    线上有 $ahead 个提交本地没有（别处部署的？先 git pull）"
  else
    echo "    线上的提交本地找不到（别处部署的？先 git fetch）"
  fi
  exit 0
fi

if [ "$MODE" = rollback ]; then
  echo "==> 回滚 $DEPLOY_HOST:$DEPLOY_DIR"
  args="rollback $RESTART $FORCE"
  remote $args
  exit $?
fi

# ---- 部署 / 试运行
echo "==> 1/4 检查本地代码"
dirty=$(git status --porcelain -- "${SOURCES[@]}" frontend)
if [ -n "$dirty" ]; then
  if [ "$ALLOW_DIRTY" != yes ]; then
    echo "    有未提交的改动（会部署的范围内）："
    printf '%s\n' "$dirty" | head -15 | sed 's/^/      /'
    echo "    先提交，或加 --allow-dirty 带着它们部署"
    exit 1
  fi
  echo "    ⚠ 带着未提交的改动部署（--allow-dirty）"
fi
if git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
  unpushed=$(git rev-list --count '@{u}..HEAD')
  [ "$unpushed" -gt 0 ] && echo "    ⚠ 本地有 $unpushed 个提交还没推送（不影响部署，记得推送）"
fi
echo "    $(git log -1 --format='%h %s')"

echo "==> 2/4 构建前端"
command -v npm >/dev/null || { echo "    找不到 npm，请先装 Node.js"; exit 1; }
if ! (cd frontend && npm run build > "$TMP/build.log" 2>&1); then
  tail -20 "$TMP/build.log"
  echo "    前端构建失败"
  exit 1
fi
echo "    完成"

echo "==> 3/4 打包"
STAGE="$TMP/stage"
mkdir -p "$STAGE/frontend"
if [ "$ALLOW_DIRTY" = yes ]; then
  git -c core.quotepath=off ls-files -z -co --exclude-standard -- "${SOURCES[@]}" \
    | tar --null -T - -cf - | tar -xf - -C "$STAGE"
else
  git archive --format=tar HEAD -- "${SOURCES[@]}" | tar -xf - -C "$STAGE"
fi
cp -r frontend/dist "$STAGE/frontend/dist"
{
  echo "commit=$(git rev-parse HEAD)"
  echo "subject=$(git log -1 --format=%s)"
  echo "branch=$(git rev-parse --abbrev-ref HEAD)"
  echo "dirty=$([ -n "$dirty" ] && echo yes || echo no)"
  echo "time=$(date '+%Y-%m-%d %H:%M:%S %z')"
  echo "by=$(git config user.name 2>/dev/null || whoami)@$(hostname | tr -d '\r')"
} > "$STAGE/DEPLOYED"
PKG="$TMP/deploy.tar.gz"
tar -czf "$PKG" -C "$STAGE" .
SHA=$(sha256sum "$PKG" | cut -d' ' -f1)
echo "    $(du -h "$PKG" | cut -f1)"

echo "==> 4/4 上传到 $DEPLOY_HOST 并$([ "$MODE" = dryrun ] && echo 比对（试运行） || echo 替换)"
upload "$PKG" || { echo "    上传失败"; exit 1; }
action=$([ "$MODE" = dryrun ] && echo dryrun || echo apply)
args="$action $RESTART $FORCE $SHA"
remote $args
