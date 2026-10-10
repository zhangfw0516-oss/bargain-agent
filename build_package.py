"""Build the Windows package: dist/BargainAgent-windows.zip.

    venv\\Scripts\\python.exe build_package.py            # exe package
    venv\\Scripts\\python.exe build_package.py --source   # source packages

``--source`` zips every committed file (git archive of HEAD, so no venv, database,
logs or .env) twice: ``BargainAgent-source.zip`` without keys (safe to publish or
hand in) and ``BargainAgent-source-with-keys.zip`` with the same .env as the exe
package. Commit first: uncommitted changes are not included.

Bundles Python and the app with PyInstaller (one folder, no install needed) and writes
the package's own .env from this checkout's .env:

* model and provider keys are copied, so the package works out of the box (anyone
  with the zip can read them; rotate them if the zip leaks);
* the sending mailbox (SMTP user/password), the recipient and the database location
  are NOT copied: each person sets their own email on the Settings page;
* daily search caps are lowered, because every copy shares the same provider quota.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
NAME = "BargainAgent"

#: Never copied into the package (personal mailbox, recipient, local paths).
PERSONAL_KEYS = {"SMTP_USER", "SMTP_PASS", "SMTP_FROM", "DEFAULT_NOTIFY_EMAIL", "DATABASE_URL"}

#: Package defaults; they replace the checkout's values.
PACKAGE_DEFAULTS = {
    "SMTP_HOST": "smtp.gmail.com",
    "SMTP_PORT": "465",
    "SERPER_DAILY_MAX_CALLS": "60",
    "SERPAPI_DAILY_MAX_CALLS": "5",
}

GUIDE = """Bargain Agent — 使用说明
========================

1. 把整个文件夹解压到任意位置（不要直接在压缩包里运行）。
2. 双击 BargainAgent.exe。会弹出一个黑色窗口，浏览器自动打开 http://127.0.0.1:8765/ 。
   使用期间黑色窗口要一直开着，关掉它程序就停了。
3. 第一次使用：打开 Settings 页
   - Notification email：填接收降价提醒的邮箱。
   - Email sending：填用来发信的邮箱和"应用专用密码"。
     Gmail：先开启两步验证，再到 https://myaccount.google.com/apppasswords 生成应用专用密码。
   不填也能搜索和比价，只是不会收到邮件提醒。
4. Find 页描述想买的东西（中英文都行），选一个结果点 Compare & monitor 建立价格监控。

演示模式：双击"演示模式.bat"。打开后有 4 个带 30 天价格历史的监控（历史是围绕真实价格模拟的，
页面顶部有 Demo data 提示），搜索和 Check now 仍是真实的。"重置演示数据.bat"让历史截止到今天。

不开窗口也自动检查价格（可选）：
   在本文件夹打开 PowerShell，运行：
   schtasks /Create /SC MINUTE /MO 30 /TN "BargainAgent check-due" /TR "\\"%CD%\\BargainAgent.exe\\" --check-due"
   删除：schtasks /Delete /TN "BargainAgent check-due" /F

说明：
- 数据（监控、价格历史）保存在本文件夹的 bargain_agent.db；升级时保留它和 .env 即可。
- 搜索额度大家共用，每台电脑每天最多 60 次搜索。
- 程序只在本机访问，不会购买任何东西。
- Windows 可能提示"已保护你的电脑"：点"更多信息" → "仍要运行"。
"""


def run(*cmd: str) -> None:
    print(">", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=ROOT)


def read_env(path: Path) -> list[tuple[str, str]]:
    pairs = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            pairs.append((key.strip(), value.strip()))
    return pairs


def package_env(source: Path) -> str:
    values = {k: v for k, v in read_env(source) if k not in PERSONAL_KEYS and v}
    values.update(PACKAGE_DEFAULTS)
    lines = ["# Bargain Agent settings. Your email settings are saved here by the Settings page."]
    lines += [f"{k}={v}" for k, v in values.items()]
    return "\n".join(lines) + "\n"


SOURCE_GUIDE = """Bargain Agent — 源代码版使用说明
================================

需要：Windows + Python 3.10 或更新版本（https://www.python.org/downloads/ ，安装时勾选 "Add python.exe to PATH"）。

1. 解压整个文件夹。
2. 双击 start.bat。第一次会自动创建 Python 环境并安装依赖（几分钟），然后打开浏览器 http://127.0.0.1:8765/ 。
   - 不带 key 的版本：会先用记事本打开 .env，填好 LLM_API_KEY 和 SERPER_API_KEY（或 SERPAPI_API_KEY）后保存，再双击 start.bat。
   - 带 key 的版本：直接就能用。
3. Settings 页填收件邮箱和发信邮箱（Gmail 用应用专用密码），才能收到降价提醒。

开发者：README.md 有架构说明；测试：venv\\Scripts\\python.exe -m unittest discover -s tests
"""


def build_source() -> int:
    if subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                      capture_output=True, text=True).stdout.strip():
        print("Note: uncommitted changes are NOT included (the package is built from the last commit).")
    BUILD.mkdir(exist_ok=True)
    DIST.mkdir(exist_ok=True)
    tracked = BUILD / "source.zip"
    run("git", "archive", "--format=zip", f"--prefix={NAME}/", "-o", str(tracked), "HEAD")
    env = package_env(ROOT / ".env") if (ROOT / ".env").exists() else None
    for suffix, with_keys in (("", False), ("-with-keys", True)):
        if with_keys and env is None:
            continue
        archive = DIST / f"{NAME}-source{suffix}.zip"
        archive.unlink(missing_ok=True)
        with zipfile.ZipFile(tracked) as src, zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
            for item in src.infolist():
                out.writestr(item, src.read(item))
            out.writestr(f"{NAME}/源代码版使用说明.txt", SOURCE_GUIDE)
            if with_keys:
                out.writestr(f"{NAME}/.env", env)
        print(f"Package: {archive} ({archive.stat().st_size / 1e6:.1f} MB)" + (" — contains API keys" if with_keys else ""))
    tracked.unlink()
    return 0


def main() -> int:
    if "--source" in sys.argv[1:]:
        return build_source()
    source_env = ROOT / ".env"
    if not source_env.exists():
        print("No .env in this checkout; fill it in first.")
        return 1
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        run(sys.executable, "-m", "pip", "install", "pyinstaller")
    run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--console",
        "--name", NAME, "--distpath", str(BUILD / "dist"), "--workpath", str(BUILD / "work"),
        "--specpath", str(BUILD),
        "--add-data", f"{ROOT / 'static'};static", "--add-data", f"{ROOT / 'prompts'};prompts",
        "--add-data", f"{ROOT / 'demo' / 'listings.json'};demo",
        "--collect-submodules", "agents", "--collect-submodules", "sources", "--collect-submodules", "demo",
        "--collect-submodules", "langgraph", "--collect-submodules", "langchain_core",
        str(ROOT / "launcher.py"))
    app = BUILD / "dist" / NAME
    (app / ".env").write_text(package_env(source_env), encoding="utf-8")
    (app / "使用说明.txt").write_text(GUIDE, encoding="utf-8")
    for name, args in (("演示模式.bat", "--demo"), ("重置演示数据.bat", "--demo --reset")):
        lines = ["@echo off", f'"%~dp0BargainAgent.exe" {args}']
        (app / name).write_bytes("".join(line + "\r\n" for line in lines).encode("ascii"))
    DIST.mkdir(exist_ok=True)
    archive = DIST / f"{NAME}-windows.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(app.rglob("*")):
            if path.is_file() and path.suffix != ".db":
                zf.write(path, Path(NAME) / path.relative_to(app))
    print(f"\nPackage: {archive} ({archive.stat().st_size / 1e6:.0f} MB)")
    print("It contains the model and provider API keys from .env — share it only with people you trust.")
    shutil.rmtree(BUILD / "work", ignore_errors=True)
    if "--team" in sys.argv[1:]:
        build_team(app)
    return 0


TEAM_GUIDE = """Bargain Agent 演示包
====================

1. 把整个压缩包解压到桌面（不要在压缩包里直接运行）。
2. 打开"演示稿"文件夹，用浏览器打开写着你名字的那个文件：你的台词（英文）、每句话时屏幕上做什么、
   你接在谁后面、讲完交给谁。
3. 排练：双击 BargainAgent 文件夹里的"演示模式.bat"，浏览器会打开演示页面
   （黑色窗口不要关）。Windows 提示"已保护你的电脑"时点"更多信息" -> "仍要运行"。
   - 页面顶部的 Demo data 提示是正常的：价格历史是演示数据，商品和当前价格是真的。
   - 搜索（Find）是真实的，需要联网，每次会用掉一点共享额度，每人每天排练别超过 10 次搜索。
   - 不要在 Settings 里改设置。排练时自己建的监控可以 Stop 后 Delete；
     4 个演示监控如果误删了，双击"重置演示数据.bat"就能恢复。
4. 正式演示用一台电脑，按演示稿的顺序进行。
"""


def build_team(app: Path) -> None:
    scripts = sorted((ROOT / "presentation" / "team").glob("*.html"))
    if not scripts:
        print("No presentation/team/*.html; run presentation/build_presentation.py first.")
        return
    top = "BargainAgent-Demo"  # ASCII: batch files misread non-ASCII folder names on some systems
    archive = DIST / "BargainAgent-team-demo.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{top}/先看我.txt", TEAM_GUIDE)
        for path in scripts:
            zf.write(path, f"{top}/演示稿/{path.name}")
        for path in sorted(app.rglob("*")):
            if path.is_file() and path.suffix != ".db":
                zf.write(path, f"{top}/{NAME}/{path.relative_to(app).as_posix()}")
    print(f"Team package: {archive} ({archive.stat().st_size / 1e6:.0f} MB) — contains API keys")


if __name__ == "__main__":
    raise SystemExit(main())
