# -*- coding: utf-8 -*-
import os, json, time, re, queue, tempfile, platform, threading
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
import pyperclip
from pynput.keyboard import Controller as KeyController, Key

import tkinter as tk
from tkinter import ttk, messagebox

from openai import OpenAI

# -------- WinAPI / pywin32 ----------
import ctypes
from ctypes import wintypes
try:
    import win32gui, win32con, win32api, win32process
except Exception:
    win32gui = win32con = win32api = win32process = None

user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None
RegisterHotKey   = user32.RegisterHotKey if user32 else None
UnregisterHotKey = user32.UnregisterHotKey if user32 else None
GetMessageW      = user32.GetMessageW if user32 else None
TranslateMessage = user32.TranslateMessage if user32 else None
DispatchMessageW = user32.DispatchMessageW if user32 else None
PostQuitMessage  = user32.PostQuitMessage if user32 else None
AllowSetForegroundWindow = getattr(user32, "AllowSetForegroundWindow", None)
AttachThreadInput = user32.AttachThreadInput if user32 else None
GetGUIThreadInfo  = user32.GetGUIThreadInfo if user32 else None
GetCursorPos      = user32.GetCursorPos if user32 else None
ScreenToClient    = user32.ScreenToClient if user32 else None

WM_HOTKEY = 0x0312
WM_PASTE  = 0x0302
MOD_CONTROL= 0x0002
MOD_SHIFT  = 0x0004
HK_ID = 1

# ------------ Config -------------
CFG_PATH = Path.home() / "ru2en.json"
DEFAULT_CFG = {
    "stt_model": "gpt-4o-mini-transcribe",  # или "gpt-4o-transcribe"
    "output_mode": "english",               # "english" | "russian"
    "style_model": "gpt-4o-mini",           # "gpt-4o-mini" | "gpt-5-mini" | "gpt-5-nano"
    "style_profile": "нейтральный",
    "auto_paste": True,                     # автопастить сразу
    "global_hotkey_enabled": True,
    "openai_api_key": "",
    "sample_rate": 16000,
    "channels": 1,
    "dtype": "int16"
}
def load_cfg():
    if CFG_PATH.exists():
        try:
            d = json.loads(CFG_PATH.read_text(encoding="utf-8"))
            x = dict(DEFAULT_CFG); x.update(d); return x
        except Exception:
            return dict(DEFAULT_CFG)
    return dict(DEFAULT_CFG)
def save_cfg(cfg):
    try:
        CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
CFG = load_cfg()

# ------------ Globals -------------
SAMPLE_RATE = int(CFG["sample_rate"]); CHANNELS = int(CFG["channels"]); DTYPE = CFG["dtype"]
recording_flag = threading.Event()
audio_q = queue.Queue()
frames = []
kb = KeyController()

ROOT = None
GUI_HWND = None
_last_window_hwnd = None    # окно чата на старте записи (через хоткей)
_last_focus_hwnd  = None    # конкретный контрол для вставки (если нашли)
_last_text = ""

_hotkey_thread = None
_hotkey_stop_evt = threading.Event()

# ------------ Audio --------------
def sd_callback(indata, frames_count, time_info, status):
    if status: pass
    audio_q.put(bytes(indata))

def start_recording(status_cb=None):
    """Старт записи. Хоткей уже сохранил активный hwnd чата в _last_window_hwnd."""
    global frames
    frames = []
    while not audio_q.empty():
        try: audio_q.get_nowait()
        except queue.Empty: break
    recording_flag.set()
    try:
        with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=0, dtype=DTYPE,
                               channels=CHANNELS, callback=sd_callback):
            if status_cb: status_cb("Запись… Говорите по-русски. Ещё раз Ctrl+Shift+R — стоп.")
            while recording_flag.is_set():
                try:
                    frames.append(np.frombuffer(audio_q.get(timeout=0.1), dtype=np.int16))
                except queue.Empty:
                    pass
    except Exception as e:
        if status_cb: status_cb(f"[ERR] Аудио: {e}")

def save_wav(np_audio, path):
    sf.write(path, np_audio, SAMPLE_RATE, subtype="PCM_16")

# ------------ OpenAI -------------
def get_client():
    key = (CFG.get("openai_api_key") or os.getenv("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("Не задан OpenAI API ключ. Введите его.")
    return OpenAI(api_key=key)

CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
def looks_like_russian(text: str) -> bool:
    return bool(CYRILLIC_RE.search(text))

def stt_transcribe(path: str) -> str:
    client = get_client()
    model = CFG["stt_model"]
    with open(path, "rb") as f:
        r = client.audio.transcriptions.create(file=f, model=model)
    return (r.text or "").strip()

STYLE_MAP = {
    "официальный": "formal, professional; no greetings",
    "нейтральный": "neutral, clear, plain; no greetings",
    "дружелюбный": "friendly yet succinct; no greetings",
    "разговорный": "casual, simple wording; no greetings",
    "лаконичный": "concise, to-the-point; no greetings",
    "академический": "academic, precise, hedged; no greetings",
}
def literal_rewrite_or_translate(text: str, target_style_ru: str, force_english: bool) -> str:
    client = get_client()
    style_hint = STYLE_MAP.get(target_style_ru, STYLE_MAP["нейтральный"])
    sysmsg = (
        "You rewrite text in STRICT LITERAL MODE to match the requested style WITHOUT changing meaning.\n"
        "HARD CONSTRAINTS:\n"
        "1) Do NOT add or remove information.\n"
        "2) Preserve named entities, numbers, code, URLs, and technical terms exactly.\n"
        "3) Sentence alignment 1:1.\n"
        "4) Keep length close to original; no fluff.\n"
        "5) Output plain text only."
    )
    user_goal = ("Translate the text to English literally, then match the style without altering meaning."
                 if force_english else
                 "Keep the language as is; only adjust form to the requested style, without altering meaning.")
    user_prompt = f"Goal: {user_goal}\nStyle: {style_hint}\nText:\n{text}"

    model = CFG["style_model"]
    if model.startswith("gpt-5"):
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role":"system","content":sysmsg},
                      {"role":"user","content":user_prompt}]
        )
    else:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role":"system","content":sysmsg},
                      {"role":"user","content":user_prompt}],
            temperature=0.0, top_p=1.0, frequency_penalty=0.0, presence_penalty=0.0
        )
    return resp.choices[0].message.content.strip()

# ------------ Win helpers -------------
def _get_foreground_hwnd():
    if not win32gui: return None
    try: return win32gui.GetForegroundWindow()
    except Exception: return None

class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus",  wintypes.HWND),
        ("hwndCapture",wintypes.HWND),
        ("hwndMenuOwner",wintypes.HWND),
        ("hwndMoveSize",wintypes.HWND),
        ("hwndCaret",   wintypes.HWND),
        ("rcCaret",     wintypes.RECT),
    ]

def _get_focus_control_from_thread(hwnd_window):
    if not (win32gui and win32process and GetGUIThreadInfo):
        return None
    try:
        tid, _ = win32process.GetWindowThreadProcessId(hwnd_window)
        g = GUITHREADINFO(); g.cbSize = ctypes.sizeof(GUITHREADINFO)
        if not GetGUIThreadInfo(tid, ctypes.byref(g)):
            return None
        for h in (g.hwndFocus, g.hwndCaret, g.hwndActive):
            if h: return h
        return None
    except Exception:
        return None

def _child_from_point(hwnd_parent, pt_screen):
    if not (win32gui and hwnd_parent and ScreenToClient):
        return None
    try:
        client_pt = win32gui.ScreenToClient(hwnd_parent, pt_screen)
        child = win32gui.ChildWindowFromPointEx(
            hwnd_parent, client_pt, 0x0001 | 0x0002  # CWP_SKIPINVISIBLE | CWP_SKIPTRANSPARENT
        )
        if child and child != hwnd_parent: return child
        return hwnd_parent
    except Exception:
        return hwnd_parent or None

def _set_foreground_and_focus(hwnd_target, candidate_focus_hwnd=None):
    if not (win32gui and win32api and win32process): 
        return False
    try:
        if AllowSetForegroundWindow:
            try: AllowSetForegroundWindow(-1)
            except Exception: pass

        win32gui.ShowWindow(hwnd_target, 5)
        time.sleep(0.08)
        try:
            win32gui.SetForegroundWindow(hwnd_target)
            time.sleep(0.08)
        except Exception:
            pass

        cur_tid = win32api.GetCurrentThreadId()
        tgt_tid, _ = win32process.GetWindowThreadProcessId(hwnd_target)
        try:
            AttachThreadInput(cur_tid, tgt_tid, True)
            win32gui.SetForegroundWindow(hwnd_target)
            time.sleep(0.08)
            focus_hwnd = candidate_focus_hwnd or _get_focus_control_from_thread(hwnd_target)
            if not focus_hwnd and GetCursorPos:
                pt = wintypes.POINT()
                if GetCursorPos(ctypes.byref(pt)):
                    focus_hwnd = _child_from_point(hwnd_target, (pt.x, pt.y))
            if focus_hwnd:
                try:
                    win32gui.SetFocus(focus_hwnd)
                    time.sleep(0.05)
                    return True
                except Exception:
                    return True
            return True
        finally:
            AttachThreadInput(cur_tid, tgt_tid, False)
    except Exception:
        return False

def _wm_paste(hwnd):
    if not (win32gui and hwnd): return False
    try:
        win32gui.SendMessage(hwnd, WM_PASTE, 0, 0)
        return True
    except Exception:
        return False

def release_modifiers():
    if not win32api: return
    try:
        for vk in (0x11, 0x12, 0x10):  # Ctrl Alt Shift
            win32api.keybd_event(vk, 0, 2, 0)  # KEYUP
    except Exception:
        pass

def _ctrl_v_win():
    if not win32api: return False
    try:
        VK_CONTROL = 0x11; VK_V = 0x56
        win32api.keybd_event(VK_CONTROL,0,0,0)
        win32api.keybd_event(VK_V,0,0,0)
        win32api.keybd_event(VK_V,0,2,0)
        win32api.keybd_event(VK_CONTROL,0,2,0)
        return True
    except Exception:
        return False

def _ctrl_v_pynput():
    try:
        with kb.pressed(Key.ctrl):
            kb.press('v'); kb.release('v')
        return True
    except Exception:
        return False

# ------------ Paste pipeline -------------
def _determine_focus_control():
    """Окно и контрол для вставки: берём окно, замеченное при старте записи."""
    global _last_focus_hwnd
    hwnd_win = _last_window_hwnd or _get_foreground_hwnd()
    if not hwnd_win:
        return None, None
    focus_hwnd = _get_focus_control_from_thread(hwnd_win)
    if not focus_hwnd and GetCursorPos:
        pt = wintypes.POINT()
        if GetCursorPos(ctypes.byref(pt)):
            focus_hwnd = _child_from_point(hwnd_win, (pt.x, pt.y))
    _last_focus_hwnd = focus_hwnd
    return hwnd_win, focus_hwnd

def paste_text(text: str):
    """Буфер → возврат фокуса → Ctrl+V (WinAPI) → Ctrl+V (pynput)."""
    hwnd_win, focus_hwnd = _determine_focus_control()
    if hwnd_win:
        _set_foreground_and_focus(hwnd_win, focus_hwnd)
        time.sleep(0.06)

    try:
        pyperclip.copy(text)
    except Exception as e:
        print(f"[WARN] pyperclip.copy: {e}")
    time.sleep(0.22)

    release_modifiers()
    time.sleep(0.02)

    # Пробуем Ctrl+V как основной и самый надежный метод.
    # WM_PASTE убран, чтобы избежать двойной вставки в приложениях типа Notepad++.
    if not _ctrl_v_win():
        _ctrl_v_pynput() # Фоллбэк на pynput, если WinAPI не сработал

# ------------ Processing -------------
def stop_and_process(status_cb=None, on_done=None):
    recording_flag.clear()
    try:
        mode_label = "Русский (без перевода)" if CFG.get("output_mode","english").lower()=="russian" \
                     else "Английский (перевод и стиль)"
        if status_cb: status_cb(f"Обработка… Режим: {mode_label}")

        if not frames:
            if status_cb: status_cb("Ничего не записано."); return
        audio_np = np.concatenate(frames, axis=0).astype(np.int16)
        duration_sec = len(audio_np) / SAMPLE_RATE
        if duration_sec < 0.5:
            if status_cb: status_cb(f"Запись слишком короткая ({duration_sec:.1f}с). Повторите."); return
        if audio_np.size == 0 or np.max(np.abs(audio_np)) < 200:
            if status_cb: status_cb("Тишина/слишком тихо. Повторите."); return

        with tempfile.TemporaryDirectory() as td:
            wav = str(Path(td) / "input.wav")
            save_wav(audio_np, wav)

            raw = stt_transcribe(wav)
            if not raw:
                if status_cb: status_cb("Пустой результат STT."); return

            if CFG.get("output_mode","english").lower() == "russian":
                final_text = raw.strip()
            else:
                force_en = looks_like_russian(raw)
                final_text = literal_rewrite_or_translate(raw, CFG["style_profile"], force_english=force_en)

        global _last_text
        _last_text = final_text
        if on_done: on_done(final_text)

        if CFG["auto_paste"]:
            paste_text(final_text)
            if status_cb: status_cb("Вставлено в активное поле.")
        else:
            if status_cb: status_cb("Готово. Используйте Ctrl+V вручную.")
    except Exception as e:
        if status_cb: status_cb(f"[ERR] {e}")

# ------------ Hotkey (WinAPI) -------------
def _toggle_record_hotkey_threadsafe():
    """WM_HOTKEY → безопасно дергаем GUI-цикл."""
    if ROOT is None:
        if not recording_flag.is_set():
            global _last_window_hwnd
            _last_window_hwnd = _get_foreground_hwnd()
            threading.Thread(target=start_recording, kwargs={"status_cb": None}, daemon=True).start()
        else:
            threading.Thread(target=stop_and_process, kwargs={"status_cb": None, "on_done": None}, daemon=True).start()
        return

    def run():
        if hasattr(ROOT, "_pull_cfg"): ROOT._pull_cfg()
        if not recording_flag.is_set():
            global _last_window_hwnd
            _last_window_hwnd = _get_foreground_hwnd()
            threading.Thread(target=start_recording, kwargs={"status_cb": ROOT.status}, daemon=True).start()
        else:
            threading.Thread(target=stop_and_process, kwargs={"status_cb": ROOT.status, "on_done": ROOT.on_done}, daemon=True).start()
    ROOT.after(0, run)

def hotkey_message_loop():
    if not (RegisterHotKey and GetMessageW):
        print("[WARN] WinAPI хоткей недоступен."); return
    if not RegisterHotKey(None, HK_ID, MOD_CONTROL | MOD_SHIFT, ord('R')):
        print("[WARN] RegisterHotKey: не удалось зарегистрировать Ctrl+Shift+R. Конфликт или нет прав.")
        return
    print("[INFO] Глобальный хоткей активен: Ctrl+Shift+R")

    msg = wintypes.MSG()
    while not _hotkey_stop_evt.is_set():
        rv = GetMessageW(ctypes.byref(msg), None, 0, 0)
        if rv == 0:
            break
        if rv == -1:
            time.sleep(0.05)
            continue
        if msg.message == WM_HOTKEY and msg.wParam == HK_ID:
            _toggle_record_hotkey_threadsafe()
        TranslateMessage(ctypes.byref(msg))
        DispatchMessageW(ctypes.byref(msg))
    UnregisterHotKey(None, HK_ID)

def start_hotkey_thread_if_enabled():
    if not CFG.get("global_hotkey_enabled", True): return
    global _hotkey_thread
    if _hotkey_thread and _hotkey_thread.is_alive(): return
    _hotkey_stop_evt.clear()
    _hotkey_thread = threading.Thread(target=hotkey_message_loop, daemon=True)
    _hotkey_thread.start()

def stop_hotkey_thread():
    _hotkey_stop_evt.set()
    try:
        if PostQuitMessage: PostQuitMessage(0)
    except Exception:
        pass

# ------------ GUI (settings only) -------------
STYLE_CHOICES = ["нейтральный", "официальный", "дружелюбный", "разговорный", "лаконичный", "академический"]
STT_CHOICES = ["gpt-4o-mini-transcribe", "gpt-4o-transcribe"]
STYLE_MODEL_CHOICES = ["gpt-4o-mini", "gpt-5-mini", "gpt-5-nano"]
OUTPUT_MODE_CHOICES = ["Английский (перевод и стиль)", "Русский (без перевода)"]

def _mode_label(v: str) -> str:
    return "Английский (перевод и стиль)" if v == "english" else "Русский (без перевода)"

INSTR_TEXT = (
    "Как пользоваться:\n"
    "1) Откройте чат и поставьте курсор в поле ввода.\n"
    "2) Нажмите Ctrl+Shift+R — начнётся запись.\n"
    "3) Снова нажмите Ctrl+Shift+R — запись остановится, текст распознается\n"
    "   и автоматически вставится в активный чат.\n"
    "Режим вывода:\n"
    "  • Английский — перевод с русской речи и стилизация.\n"
    "  • Русский — распознавание без перевода.\n"
    "Важно: некоторые чаты блокируют автоматику. Тогда используйте ручной Ctrl+V."
)

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        global ROOT, GUI_HWND
        ROOT = self

        self.title("RU→EN / RU→RU (OpenAI) — хоткей-режим")
        self.geometry("780x560"); self.resizable(False, False)
        try: self.tk.call('tk','scaling',1.2)
        except Exception: pass

        try: GUI_HWND = self.winfo_id()
        except Exception: GUI_HWND = None

        pad={'padx':10,'pady':6}
        self.status_var=tk.StringVar(value="Готов. Горячая клавиша: Ctrl+Shift+R")

        r=0
        ttk.Label(self,text="Режим вывода:").grid(column=0,row=r,sticky="w",**pad)
        self.cb_mode=ttk.Combobox(self,values=OUTPUT_MODE_CHOICES,state="readonly",width=32)
        self.cb_mode.set(_mode_label(CFG.get("output_mode","english"))); self.cb_mode.grid(column=1,row=r,sticky="w",**pad)

        ttk.Label(self,text="STT-модель:").grid(column=0,row=r+1,sticky="w",**pad)
        self.cb_stt=ttk.Combobox(self,values=STT_CHOICES,state="readonly",width=32)
        self.cb_stt.set(CFG["stt_model"]); self.cb_stt.grid(column=1,row=r+1,sticky="w",**pad)

        ttk.Label(self,text="Модель стиля (для EN):").grid(column=0,row=r+2,sticky="w",**pad)
        self.cb_style_model=ttk.Combobox(self,values=STYLE_MODEL_CHOICES,state="readonly",width=32)
        self.cb_style_model.set(CFG["style_model"]); self.cb_style_model.grid(column=1,row=r+2,sticky="w",**pad)

        ttk.Label(self,text="Профиль стиля (для EN):").grid(column=0,row=r+3,sticky="w",**pad)
        self.cb_style=ttk.Combobox(self,values=STYLE_CHOICES,state="readonly",width=32)
        self.cb_style.set(CFG["style_profile"]); self.cb_style.grid(column=1,row=r+3,sticky="w",**pad)

        # --- OpenAI API ключ ---
        ttk.Label(self,text="OpenAI API ключ:").grid(column=0,row=r+4,sticky="w",**pad)

        self.key_var = tk.StringVar(value=CFG.get("openai_api_key",""))
        self.entry_key = ttk.Entry(self, textvariable=self.key_var, width=44, show="•")
        self.entry_key.grid(column=1,row=r+4,sticky="w",**pad)

        # Контекстное меню «Вставить»
        self._key_menu = tk.Menu(self, tearoff=0)
        self._key_menu.add_command(label="Вставить", command=lambda: self.entry_key.event_generate("<<Paste>>"))
        self.entry_key.bind("<Button-3>", lambda e: self._key_menu.tk_popup(e.x_root, e.y_root))

        # Явная обработка Ctrl+V для поля (иногда системное не срабатывает)
        self.entry_key.bind("<Control-v>", lambda e: (self.entry_key.event_generate("<<Paste>>"), "break"))

        # Кнопка «Вставить из буфера»
        self.btn_paste_key = ttk.Button(self, text="Вставить из буфера", command=self.paste_key_from_clipboard)
        self.btn_paste_key.grid(column=1,row=r+5,sticky="w",**pad)

        # Показать/скрыть ключ
        self.var_show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(self, text="Показать ключ", variable=self.var_show_key, command=self.toggle_key_visibility)\
            .grid(column=1,row=r+6,sticky="w",**pad)

        # Индикатор длины/хвоста
        self.key_info_var = tk.StringVar(value=self._format_key_info(self.key_var.get()))
        ttk.Label(self, textvariable=self.key_info_var, foreground="#444")\
            .grid(column=1,row=r+7,sticky="w",**pad)
        self.key_var.trace_add("write", lambda *_: self.key_info_var.set(self._format_key_info(self.key_var.get())))

        # Инструкция
        ttk.Label(self,text="Краткая инструкция:").grid(column=0,row=r+8,sticky="nw",**pad)
        self.instr=tk.Text(self,height=10,width=70,wrap="word")
        self.instr.insert("1.0", INSTR_TEXT); self.instr.config(state="disabled")
        self.instr.grid(column=0,row=r+9,columnspan=2,sticky="we",padx=10,pady=(0,6))

        # Кнопки
        self.btn_save=ttk.Button(self,text="Сохранить настройки",command=self.save_settings)
        self.btn_save.grid(column=0,row=r+10,sticky="w",**pad)

        self.btn_quit=ttk.Button(self,text="Выход",command=self.on_quit)
        self.btn_quit.grid(column=1,row=r+10,sticky="e",**pad)

        ttk.Label(self,textvariable=self.status_var,foreground="#006400")\
            .grid(column=0,row=r+11,columnspan=2,sticky="w",**pad)

        # хоткей
        self.after(200, self._start_hotkey)
        self.print_banner()

    # --- helpers GUI ---
    def _start_hotkey(self):
        CFG["global_hotkey_enabled"]=True
        start_hotkey_thread_if_enabled()

    def print_banner(self):
        print("RU→EN / RU→RU — хоткей-режим")
        print("Hotkey: Ctrl+Shift+R", "(вкл)" if CFG.get("global_hotkey_enabled",True) else "(выкл)")
        print(f"Mode={CFG.get('output_mode','english')} | STT={CFG['stt_model']} | StyleModel={CFG['style_model']} | Style={CFG['style_profile']} | AutoPaste={CFG.get('auto_paste',True)}")

    def status(self,msg): 
        self.status_var.set(msg); self.update_idletasks()

    def _format_key_info(self, key: str) -> str:
        s = key.strip()
        if not s:
            return "Ключ: не задан"
        tail = s[-4:] if len(s) >= 4 else s
        return f"Ключ: {len(s)} символов, оканчивается на …{tail}"

    def toggle_key_visibility(self):
        self.entry_key.config(show="" if self.var_show_key.get() else "•")

    def paste_key_from_clipboard(self):
        try:
            txt = pyperclip.paste()
        except Exception as e:
            messagebox.showwarning("Буфер обмена", f"Не удалось прочитать буфер: {e}")
            return
        if txt is None:
            return
        txt = str(txt).strip()
        self.entry_key.delete(0, tk.END)
        self.entry_key.insert(0, txt)

    def _pull_cfg(self):
        CFG["output_mode"]     = "english" if self.cb_mode.get().startswith("Английский") else "russian"
        CFG["stt_model"]       = self.cb_stt.get()
        CFG["style_model"]     = self.cb_style_model.get()
        CFG["style_profile"]   = self.cb_style.get()
        CFG["auto_paste"]      = True
        CFG["global_hotkey_enabled"]= True
        CFG["openai_api_key"]  = self.key_var.get().strip()

    def save_settings(self):
        self._pull_cfg(); save_cfg(CFG)
        messagebox.showinfo("Сохранено", f"Настройки сохранены в {CFG_PATH}")

    def on_quit(self):
        try: stop_hotkey_thread()
        except Exception: pass
        self.destroy()

    def on_done(self, text): pass  # совместимость с коллбеком

# ------------- Main -------------
def main():
    if platform.system().lower() == "windows":
        pass
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_quit)
    app.mainloop()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        try: stop_hotkey_thread()
        except Exception: pass