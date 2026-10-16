# -*- coding: utf-8 -*-
"""트렌드 템플릿 스캐너 - PC 앱 (바탕화면 아이콘으로 실행)"""
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
CONFIG = BASE / "config.json"
REPORT = BASE / "트렌드스캐너.html"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
FONT = "Malgun Gothic" if os.name == "nt" else "TkDefaultFont"
NAMES = {"kr": "한국", "us": "미국"}


def console_python():
    """창 없이 실행된 경우(pythonw)에도 출력을 받을 수 있게 python.exe를 쓴다."""
    exe = Path(sys.executable)
    alt = exe.with_name("python.exe")
    return str(alt if alt.exists() else exe)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("트렌드 템플릿 스캐너")
        self.geometry("720x640")
        self.minsize(560, 520)
        try:
            self.iconbitmap(str(BASE / "assets" / "app.ico"))
        except Exception:
            pass
        self.q = queue.Queue()
        self.proc = None
        self.cfg = load_json(CONFIG, {})
        self._style()
        self._build()
        self.refresh_status()
        self.after(150, self._poll)

    # ------------------------------------------------------------ 화면
    def _style(self):
        s = ttk.Style(self)
        try:
            s.theme_use("vista" if os.name == "nt" else "clam")
        except Exception:
            pass
        s.configure(".", font=(FONT, 10))
        s.configure("Title.TLabel", font=(FONT, 16, "bold"))
        s.configure("Big.TLabel", font=(FONT, 20, "bold"))
        s.configure("Muted.TLabel", foreground="#66706a")
        s.configure("Accent.TButton", font=(FONT, 10, "bold"))

    def _build(self):
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)

        head = ttk.Frame(root)
        head.pack(fill="x")
        ttk.Label(head, text="트렌드 템플릿 스캐너", style="Title.TLabel").pack(side="left")
        ttk.Button(head, text="결과 화면 열기", style="Accent.TButton", command=self.open_report).pack(side="right")
        self.phone_btn = ttk.Button(head, text="휴대폰 앱 주소", command=self.open_phone)
        self.phone_btn.pack(side="right", padx=6)

        # 시장별 요약
        cards = ttk.Frame(root)
        cards.pack(fill="x", pady=(14, 6))
        self.card = {}
        for i, m in enumerate(("kr", "us")):
            f = ttk.LabelFrame(cards, text=f" {NAMES[m]} ", padding=10)
            f.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0))
            cards.columnconfigure(i, weight=1)
            big = ttk.Label(f, text="—", style="Big.TLabel")
            big.pack(anchor="w")
            sub = ttk.Label(f, text="", style="Muted.TLabel", justify="left")
            sub.pack(anchor="w")
            btn = ttk.Button(f, text=f"{NAMES[m]} 지금 스캔", command=lambda m=m: self.run_scan(m))
            btn.pack(anchor="w", pady=(8, 0))
            self.card[m] = (big, sub, btn)

        bar = ttk.Frame(root)
        bar.pack(fill="x", pady=(6, 4))
        self.all_btn = ttk.Button(bar, text="한국 + 미국 모두 스캔", command=lambda: self.run_scan("all"))
        self.all_btn.pack(side="left")
        self.stop_btn = ttk.Button(bar, text="중지", command=self.stop_scan, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        self.pb = ttk.Progressbar(bar, mode="indeterminate")
        self.pb.pack(side="left", fill="x", expand=True, padx=(6, 0))

        # 진행 기록
        logf = ttk.Frame(root)
        logf.pack(fill="both", expand=True, pady=(4, 10))
        self.log = tk.Text(logf, height=10, wrap="word", font=("Consolas" if os.name == "nt" else "TkFixedFont", 9),
                           relief="flat", background="#f3f5f3", padx=8, pady=6)
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set, state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # 설정
        st = ttk.LabelFrame(root, text=" 설정 ", padding=10)
        st.pack(fill="x")
        ttk.Label(st, text="한국 최소 시가총액").grid(row=0, column=0, sticky="w")
        self.kr_cap = tk.StringVar(value=str(self.cfg.get("kr_min_cap_eok", 3000)))
        ttk.Entry(st, textvariable=self.kr_cap, width=8, justify="right").grid(row=0, column=1, padx=4)
        ttk.Label(st, text="억 원").grid(row=0, column=2, sticky="w", padx=(0, 18))
        ttk.Label(st, text="미국 최소 시가총액").grid(row=0, column=3, sticky="w")
        self.us_cap = tk.StringVar(value=str(self.cfg.get("us_min_cap_eok_usd", 20)))
        ttk.Entry(st, textvariable=self.us_cap, width=6, justify="right").grid(row=0, column=4, padx=4)
        ttk.Label(st, text="억 달러").grid(row=0, column=5, sticky="w")
        ttk.Label(st, text="휴대폰 앱 주소").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.phone = tk.StringVar(value=self.cfg.get("phone_url", ""))
        ttk.Entry(st, textvariable=self.phone).grid(row=1, column=1, columnspan=5, sticky="ew", padx=4, pady=(8, 0))
        st.columnconfigure(5, weight=1)
        ttk.Button(st, text="저장", command=self.save_cfg).grid(row=0, column=6, rowspan=2, padx=(12, 0), sticky="ns")
        self.auto = tk.BooleanVar(value=self.tasks_registered())
        ttk.Checkbutton(st, text="평일 자동 스캔 (한국 17:50 · 미국 화~토 07:20, PC가 켜져 있을 때)",
                        variable=self.auto, command=self.toggle_auto).grid(row=2, column=0, columnspan=7, sticky="w", pady=(10, 0))
        self._phone_state()

    # ------------------------------------------------------------ 상태
    def refresh_status(self):
        hist = load_json(DATA / "history.json", [])
        for m in ("kr", "us"):
            big, sub, _ = self.card[m]
            cur = load_json(DATA / f"{m}_latest.json", None)
            if not cur:
                big.config(text="스캔 전")
                sub.config(text="아래 버튼으로 첫 스캔을 하세요.")
                continue
            s = cur["summary"]
            h = [x for x in hist if x["market"] == m]
            d = (s["pass"] - h[-2]["pass"]) if len(h) > 1 else None
            pat = s.get("patterns", 0)
            big.config(text=f"{s['pass']}종목 통과")
            sub.config(text=(f"기준일 {s['asOf']}" + (" (장중 시세)" if s.get("intraday") else " 종가")
                             + (f" · 직전 대비 {d:+d}" if d is not None else "")
                             + f"\nVCP {s['vcp']} · 베이스 패턴 {pat} · 스캔 {s['universe']:,}종목"))

    def tasks_registered(self):
        if os.name != "nt":
            return False
        r = subprocess.run(["schtasks", "/query", "/tn", "TrendScanner_KR"], capture_output=True, creationflags=NO_WINDOW)
        return r.returncode == 0

    # ------------------------------------------------------------ 동작
    def append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_busy(self, busy):
        for _, _, b in self.card.values():
            b.config(state="disabled" if busy else "normal")
        self.all_btn.config(state="disabled" if busy else "normal")
        self.stop_btn.config(state="normal" if busy else "disabled")
        (self.pb.start(12) if busy else self.pb.stop())

    def run_scan(self, market):
        if self.proc:
            return
        self.save_cfg(quiet=True)
        self.append(f"\n▶ {NAMES.get(market, '한국 + 미국')} 스캔 시작 (한국 1~3분, 미국 5~10분)\n")
        self.set_busy(True)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen([console_python(), str(BASE / "trend_scanner.py"), market], cwd=BASE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                     creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _reader(self, proc):
        for line in proc.stdout:
            # "2026-10-16 17:50:01,123 INFO [kr] ..." → 시각과 수준 표시는 줄여서 보여준다
            parts = line.split(" ", 3)
            self.q.put(("line", (parts[1][:8] + "  " + parts[3]) if len(parts) == 4 and parts[2] in ("INFO", "WARNING", "ERROR") else line))
        self.q.put(("done", proc.wait()))

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "line":
                    self.append(val)
                else:
                    self.proc = None
                    self.set_busy(False)
                    self.refresh_status()
                    if val == 0:
                        self.append("✔ 완료. '결과 화면 열기'로 확인하세요.\n")
                    else:
                        self.append("✖ 문제가 있었습니다. 위 기록이나 logs\\scan.log 를 Claude에게 보여주세요.\n")
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def stop_scan(self):
        if self.proc:
            self.proc.terminate()
            self.append("■ 중지했습니다.\n")

    def open_report(self):
        if REPORT.exists():
            webbrowser.open(REPORT.as_uri())
        else:
            messagebox.showinfo("결과 없음", "아직 스캔 결과가 없습니다. 먼저 스캔하세요.")

    def open_phone(self):
        url = self.phone.get().strip()
        if url:
            webbrowser.open(url)

    def _phone_state(self):
        self.phone_btn.config(state="normal" if self.phone.get().strip() else "disabled")

    def save_cfg(self, quiet=False):
        try:
            kr, us = float(self.kr_cap.get().replace(",", "")), float(self.us_cap.get().replace(",", ""))
            if kr <= 0 or us <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("설정", "시가총액 기준은 0보다 큰 숫자로 입력하세요.")
            return
        self.cfg.update({"kr_min_cap_eok": kr, "us_min_cap_eok_usd": us, "phone_url": self.phone.get().strip()})
        CONFIG.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=1), encoding="utf-8")
        self._phone_state()
        if not quiet:
            self.append(f"설정 저장: 한국 {kr:,.0f}억 원 이상, 미국 {us:,.0f}억 달러 이상 (다음 스캔부터 적용)\n")

    def toggle_auto(self):
        if os.name != "nt":
            return
        if self.auto.get():
            pyw = Path(sys.executable).with_name("pythonw.exe")
            runner = str(pyw if pyw.exists() else Path(sys.executable))
            cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BASE / "register_tasks.ps1"),
                   "-Python", runner, "-Script", str(BASE / "trend_scanner.py")]
        else:
            cmd = ["powershell", "-NoProfile", "-Command",
                   "Unregister-ScheduledTask -TaskName 'TrendScanner_KR' -Confirm:$false -ErrorAction SilentlyContinue;"
                   "Unregister-ScheduledTask -TaskName 'TrendScanner_US' -Confirm:$false -ErrorAction SilentlyContinue"]
        r = subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)
        ok = self.tasks_registered()
        self.auto.set(ok)
        self.append(("자동 스캔 켜짐\n" if ok else "자동 스캔 꺼짐\n") if r.returncode == 0 else f"자동 스캔 설정 실패: {r.stderr.strip()[:300]}\n")


if __name__ == "__main__":
    App().mainloop()
