# -*- coding: utf-8 -*-
"""트렌드 템플릿 스캐너 설치: 라이브러리 설치 → 작업 스케줄러 등록 → 첫 스캔"""
import os
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def step(n, msg):
    print(f"\n [{n}/5] {msg}", flush=True)


def make_shortcut():
    """바탕화면에 '트렌드 스캐너' 아이콘을 만든다 (창 없는 pythonw로 app.pyw 실행)."""
    pyw = Path(sys.executable).with_name("pythonw.exe")
    target = str(pyw if pyw.exists() else Path(sys.executable))
    ps = (
        "$d=[Environment]::GetFolderPath('Desktop');"
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d '트렌드 스캐너.lnk'));"
        f"$s.TargetPath='{target}';"
        f"$s.Arguments='\"{BASE / 'app.pyw'}\"';"
        f"$s.WorkingDirectory='{BASE}';"
        f"$s.IconLocation='{BASE / 'assets' / 'app.ico'}';"
        "$s.Description='트렌드 템플릿 스캐너';$s.Save()"
    )
    return subprocess.run(["powershell", "-NoProfile", "-Command", ps]).returncode == 0


def main():
    print("\n 트렌드 템플릿 스캐너 설치\n ------------------------------------------")
    step(1, "파이썬 확인")
    print(f"       {sys.version.split()[0]}  ({sys.executable})")
    if sys.version_info < (3, 9):
        print(" 파이썬 3.9 이상이 필요합니다.")
        return 1

    step(2, "라이브러리 설치 (yfinance, pandas, numpy) - 1~2분 걸릴 수 있습니다")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check",
                        "yfinance", "pandas", "numpy"])
    if r.returncode != 0:
        print("\n 라이브러리 설치에 실패했습니다. 위 메시지를 캡처해서 Claude에게 보여주세요.")
        return 1

    step(3, "작업 스케줄러 등록 (한국장 월~금 17:50, 미국장 화~토 07:20)")
    pyw = Path(sys.executable).with_name("pythonw.exe")
    runner = str(pyw if pyw.exists() else Path(sys.executable))
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(BASE / "register_tasks.ps1"), "-Python", runner, "-Script", str(BASE / "trend_scanner.py")])
    if r.returncode != 0:
        print("\n 자동 실행 등록에 실패했습니다. 위 메시지를 캡처해서 Claude에게 보여주세요.")
        print(" (스캔 자체는 '지금 스캔.bat'으로 계속 쓸 수 있습니다.)")

    step(4, "바탕화면에 '트렌드 스캐너' 아이콘 만들기")
    print("       완료" if make_shortcut() else "       아이콘을 만들지 못했습니다. 폴더의 app.pyw를 더블클릭해도 앱이 열립니다.")

    step(5, "첫 스캔 (한국+미국, 5~15분 걸립니다. 이 창을 닫지 마세요)")
    r = subprocess.run([sys.executable, str(BASE / "trend_scanner.py"), "all", "--open"], cwd=BASE)
    print("\n ------------------------------------------")
    if r.returncode == 0:
        print(" 설치 완료. 이제 매일 자동으로 스캔합니다.")
        print(" 바탕화면의 '트렌드 스캐너' 아이콘으로 앱을 여세요.")
    else:
        print(" 설치는 끝났지만 첫 스캔 중 문제가 있었습니다. logs\\scan.log 를 Claude에게 보여주세요.")
    print(f" 결과 화면: {BASE / '트렌드스캐너.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
