#!/usr/bin/env bash
# 在 Linux 桌面上安装「科研工具箱」启动器：写入应用菜单，并在 KDE Plasma 下固定到任务栏。
# 用法：./scripts/install_linux_launcher.sh [--no-pin] [--uninstall]

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ID="research-toolkit"
APPS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_FILE="$APPS_DIR/$APP_ID.desktop"
ICON="$PROJECT_ROOT/static/toolbox.png"
LAUNCHER="$PROJECT_ROOT/run_web_panel.sh"
PIN=1
UNINSTALL=0

for arg in "$@"; do
    case "$arg" in
        --no-pin) PIN=0 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help) sed -n '2,3p' "${BASH_SOURCE[0]}" | sed 's/^# //'; exit 0 ;;
        *) printf '未知参数：%s\n' "$arg" >&2; exit 2 ;;
    esac
done

# 通过 Plasma 脚本接口增删任务栏固定项；直接改配置文件会被运行中的 plasmashell 覆盖。
plasma_pin() {
    local action="$1" qdbus_bin
    qdbus_bin="$(command -v qdbus6 || command -v qdbus || true)"
    [[ -n "$qdbus_bin" ]] || return 1
    "$qdbus_bin" org.kde.plasmashell /PlasmaShell org.kde.PlasmaShell.evaluateScript "
        var entry = 'applications:$APP_ID.desktop';
        var touched = 0;
        panels().forEach(function (panel) {
            panel.widgets().forEach(function (widget) {
                if (widget.type !== 'org.kde.plasma.icontasks' && widget.type !== 'org.kde.plasma.taskmanager') return;
                widget.currentConfigGroup = ['General'];
                var raw = String(widget.readConfig('launchers') || '');
                var list = raw.split(',').filter(function (item) { return item && item !== entry; });
                if ('$action' === 'add') list.push(entry);
                widget.writeConfig('launchers', list.join(','));
                widget.reloadConfig();
                touched += 1;
            });
        });
        print(touched);
    "
}

if [[ "$UNINSTALL" -eq 1 ]]; then
    plasma_pin remove >/dev/null 2>&1 || true
    rm -f "$DESKTOP_FILE"
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
    printf '已移除启动器：%s\n' "$DESKTOP_FILE"
    exit 0
fi

[[ -f "$ICON" ]] || { printf '缺少图标 %s，请先运行 python scripts/make_toolbox_icon.py\n' "$ICON" >&2; exit 1; }
[[ -x "$LAUNCHER" ]] || chmod +x "$LAUNCHER"

mkdir -p "$APPS_DIR"
cat > "$DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Version=1.0
Name=Research Toolkit
Name[zh_CN]=科研工具箱
Comment=Local research toolkit web panel
Comment[zh_CN]=启动本机科研工具箱网页面板
Exec="$LAUNCHER"
Path=$PROJECT_ROOT
Icon=$ICON
Terminal=true
Categories=Education;Science;
Keywords=research;paper;pdf;科研;论文;
StartupNotify=false
DESKTOP
chmod +x "$DESKTOP_FILE"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
printf '已写入启动器：%s\n' "$DESKTOP_FILE"

if [[ "$PIN" -eq 1 ]]; then
    count="$(plasma_pin add 2>/dev/null | tr -d '[:space:]' || true)"
    if [[ "$count" =~ ^[1-9][0-9]*$ ]]; then
        printf '已固定到 KDE 任务栏。\n'
        # 运行中的任务栏不一定会立刻重读固定项，看不到图标时需要重启桌面外壳。
        printf '如果任务栏上没有出现图标，请运行：systemctl --user restart plasma-plasmashell.service\n'
    else
        printf '未能自动固定到任务栏（非 KDE Plasma 或没有任务管理器部件）。请在应用菜单里找到「科研工具箱」，右键选择固定到任务栏。\n'
    fi
fi
