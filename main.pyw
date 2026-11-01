"""Заряд мышки Razer DeathAdder V3 HyperSpeed без Synapse.

HID дёргается через ctypes (hid.dll / setupapi.dll), окно на tkinter,
иконка в трее на pystray. Протокол Razer взят из открытого проекта OpenRazer.

Производительность: мышь опрашивается раз в POLL_SECONDS, остальное время поток спит.
Подключение/отключение провода Windows сообщает сама (CM_Register_Notification),
поэтому реакция мгновенная без частого опроса.
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import threading
import time
import tkinter as tk

import pystray
from PIL import Image, ImageDraw

VID = 0x1532
PIDS = (0x00C4, 0x00C5)               # DeathAdder V3 HyperSpeed: провод, донгл
TRANSACTION_IDS = (0x1F, 0xFF, 0x3F, 0x9F)  # перебираем, пока мышь не ответит
POLL_SECONDS = 60
SPEC_HOURS = 100                      # паспортное время работы, пока нет своей статистики

FROZEN = getattr(sys, "frozen", False)   # запущен как exe из PyInstaller
APP_DIR = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)
STATS_FILE = os.path.join(APP_DIR, "stats.json")
ICON_FILE = os.path.join(RES_DIR, "icon.ico")
MUTEX_NAME = "Local\\MouseBattery.Instance"
SHOW_EVENT_NAME = "Local\\MouseBattery.Show"


# ---------------------------------------------------------------- WinAPI

class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", wt.DWORD), ("Reserved", ctypes.c_size_t)]


class _CM_FILTER_UNION(ctypes.Union):
    _fields_ = [("ClassGuid", GUID), ("InstanceId", ctypes.c_wchar * 200)]


class CM_NOTIFY_FILTER(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("Flags", wt.DWORD), ("FilterType", wt.DWORD),
                ("Reserved", wt.DWORD), ("u", _CM_FILTER_UNION)]


setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
hid = ctypes.WinDLL("hid", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
cfgmgr32 = ctypes.WinDLL("cfgmgr32")

INVALID_HANDLE = ctypes.c_void_p(-1).value
DIGCF_PRESENT, DIGCF_DEVICEINTERFACE = 0x02, 0x10
GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_RW = 0x01 | 0x02
OPEN_EXISTING = 3
ERROR_ALREADY_EXISTS = 183
INFINITE = 0xFFFFFFFF
EVENT_MODIFY_STATE = 0x0002

hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
hid.HidD_SetFeature.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.ULONG]
hid.HidD_SetFeature.restype = wt.BOOLEAN
hid.HidD_GetFeature.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.ULONG]
hid.HidD_GetFeature.restype = wt.BOOLEAN
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), wt.LPCWSTR, wt.HWND, wt.DWORD]
setupapi.SetupDiGetClassDevsW.restype = wt.HANDLE
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
    wt.HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), wt.DWORD,
    ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
    wt.HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p, wt.DWORD,
    ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wt.HANDLE]
kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                 wt.DWORD, wt.DWORD, wt.HANDLE]
kernel32.CreateFileW.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
kernel32.CreateEventW.restype = wt.HANDLE
kernel32.OpenEventW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
kernel32.OpenEventW.restype = wt.HANDLE
kernel32.SetEvent.argtypes = [wt.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.WaitForSingleObject.restype = wt.DWORD

CM_NOTIFY_CALLBACK = ctypes.WINFUNCTYPE(wt.DWORD, wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                                        ctypes.c_void_p, wt.DWORD)
cfgmgr32.CM_Register_Notification.argtypes = [ctypes.POINTER(CM_NOTIFY_FILTER), ctypes.c_void_p,
                                              CM_NOTIFY_CALLBACK, ctypes.POINTER(wt.HANDLE)]
cfgmgr32.CM_Register_Notification.restype = wt.DWORD


def hid_guid():
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    return guid


def hid_paths():
    guid = hid_guid()
    devs = setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None,
                                         DIGCF_PRESENT | DIGCF_DEVICEINTERFACE)
    if devs == INVALID_HANDLE:
        return []
    paths = []
    try:
        i = 0
        while True:
            data = SP_DEVICE_INTERFACE_DATA()
            data.cbSize = ctypes.sizeof(data)
            if not setupapi.SetupDiEnumDeviceInterfaces(devs, None, ctypes.byref(guid), i,
                                                        ctypes.byref(data)):
                break
            i += 1
            size = wt.DWORD()
            setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(data), None, 0,
                                                      ctypes.byref(size), None)
            buf = ctypes.create_string_buffer(size.value)
            # cbSize у SP_DEVICE_INTERFACE_DETAIL_DATA_W: 8 на x64, 6 на x86
            ctypes.cast(buf, ctypes.POINTER(wt.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if setupapi.SetupDiGetDeviceInterfaceDetailW(devs, ctypes.byref(data), buf, size,
                                                         None, None):
                paths.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(devs)
    return paths


def open_device(path):
    for access in (GENERIC_READ | GENERIC_WRITE, 0):
        h = kernel32.CreateFileW(path, access, FILE_SHARE_RW, None, OPEN_EXISTING, 0, None)
        if h != INVALID_HANDLE:
            return h
    return None


class DeviceWatcher:
    """Будит event при появлении/пропаже любого HID-интерфейса. Колбэк зовёт сама Windows."""

    def __init__(self, event):
        self.event = event
        self.handle = wt.HANDLE()
        self._cb = CM_NOTIFY_CALLBACK(self._on_change)   # держим ссылку, иначе GC
        flt = CM_NOTIFY_FILTER()
        flt.cbSize = ctypes.sizeof(flt)
        flt.FilterType = 0                                # CM_NOTIFY_FILTER_TYPE_DEVICEINTERFACE
        flt.u.ClassGuid = hid_guid()
        cfgmgr32.CM_Register_Notification(ctypes.byref(flt), None, self._cb,
                                          ctypes.byref(self.handle))

    def _on_change(self, h_notify, context, action, data, size):
        self.event.set()
        return 0


# ---------------------------------------------------------------- протокол Razer

STATUS_BUSY, STATUS_OK = 0x01, 0x02


def build_report(tid, cmd_class, cmd_id, size):
    r = bytearray(90)
    r[1] = tid
    r[5] = size
    r[6] = cmd_class
    r[7] = cmd_id
    crc = 0
    for b in r[2:88]:
        crc ^= b
    r[88] = crc
    return r


def transact(handle, report):
    out = (ctypes.c_ubyte * 91)(0, *report)   # первый байт — report id 0
    if not hid.HidD_SetFeature(handle, out, 91):
        return None
    for _ in range(10):
        time.sleep(0.06)
        inp = (ctypes.c_ubyte * 91)()
        if not hid.HidD_GetFeature(handle, inp, 91):
            return None
        resp = bytes(inp[1:])
        if resp[0] != STATUS_BUSY:
            return resp
    return None


class RazerMouse:
    def __init__(self):
        self.handle = None
        self.tid = None
        self.path = None

    def close(self):
        if self.handle:
            kernel32.CloseHandle(self.handle)
        self.handle = None
        self.tid = None
        self.path = None

    def _query(self, cmd_class, cmd_id, tid=None):
        tid = self.tid if tid is None else tid
        resp = transact(self.handle, build_report(tid, cmd_class, cmd_id, 0x02))
        if resp and resp[0] == STATUS_OK and resp[6] == cmd_class and resp[7] == cmd_id:
            return resp[9]          # arguments[1]
        return None

    @staticmethod
    def _candidates():
        def pid_rank(path):
            for i, pid in enumerate(PIDS):
                if f"vid_{VID:04x}&pid_{pid:04x}" in path:
                    return i
            return None

        cands = [p for p in hid_paths() if pid_rank(p.lower()) is not None]
        # сначала провод, затем донгл; внутри — MI_00 без коллекций (управляющий интерфейс)
        cands.sort(key=lambda p: (pid_rank(p.lower()), "mi_00#" not in p.lower(),
                                  "mi_00" not in p.lower()))
        return cands

    def _connect(self, cands):
        for path in cands:
            h = open_device(path)
            if not h:
                continue
            self.handle = h
            for tid in TRANSACTION_IDS:
                if self._query(0x07, 0x80, tid) is not None:
                    self.tid = tid
                    self.path = path
                    return True
            self.close()
        return False

    def read(self, rescan):
        """(процент, заряжается) или None. rescan — устройства поменялись, ищем заново."""
        if rescan or not self.handle:
            cands = self._candidates()
            # воткнули/выдернули провод — переключаемся на более приоритетное устройство
            if self.handle and (not cands or cands[0] != self.path):
                self.close()
            if not self.handle and not self._connect(cands):
                return None
        raw = self._query(0x07, 0x80)
        if raw is None:
            self.close()
            return None
        charging = self._query(0x07, 0x84) == 1
        return round(raw / 255 * 100), charging


# ---------------------------------------------------------------- оценка времени

class Estimator:
    """Копит «активное время» и «потраченные проценты», только пока программа работает,
    чтобы выключенный комп не портил статистику."""

    SAVE_EVERY = 600

    def __init__(self):
        self.used_pct = 0.0
        self.active_sec = 0.0
        self.last = None   # (ts, pct)
        self.saved_at = time.time()
        try:
            with open(STATS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            self.used_pct, self.active_sec = d["used_pct"], d["active_sec"]
        except (OSError, ValueError, KeyError):
            pass

    def update(self, pct, charging):
        now = time.time()
        if charging:
            self.last = None
            return
        dropped = False
        if self.last:
            dt = now - self.last[0]
            if dt <= POLL_SECONDS * 3:
                self.active_sec += dt
                if pct < self.last[1]:
                    self.used_pct += self.last[1] - pct
                    dropped = True
        self.last = (now, pct)
        if self.used_pct > 30:       # держим статистику свежей
            self.used_pct /= 2
            self.active_sec /= 2
        if dropped or now - self.saved_at > self.SAVE_EVERY:
            self.save()

    def save(self):
        self.saved_at = time.time()
        try:
            with open(STATS_FILE, "w", encoding="utf-8") as f:
                json.dump({"used_pct": self.used_pct, "active_sec": self.active_sec}, f)
        except OSError:
            pass

    def remaining_hours(self, pct):
        if self.used_pct >= 3 and self.active_sec > 0:
            return pct / (self.used_pct / self.active_sec) / 3600, False
        return pct / 100 * SPEC_HOURS, True


def fmt_hours(h):
    if h < 1:
        return f"{max(1, round(h * 60))} мин"
    d, hh = divmod(round(h), 24)
    return f"{d} д {hh} ч" if d else f"{hh} ч"


# ---------------------------------------------------------------- UI

BG = "#1b1d22"
FG = "#e8e8e8"
DIM = "#8a8f98"
GREEN, ORANGE, RED, WHITE = "#4caf50", "#ff9800", "#f44336", "#f5f5f5"
BOLT = "#ffd600"


def level_color(pct, charging):
    if charging:
        return WHITE
    if pct < 25:
        return RED
    if pct < 60:
        return ORANGE
    return GREEN


def tray_image(pct, charging):
    """Маленькая батарейка для трея (рисуем крупно, Windows сама ужмёт)."""
    s = 64
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((2, 14, 54, 50), outline=FG, width=5)
    d.rectangle((56, 24, 62, 40), fill=FG)
    if pct is None:
        d.line((14, 24, 42, 40), fill=DIM, width=5)
        d.line((14, 40, 42, 24), fill=DIM, width=5)
        return img
    w = round(44 * max(0, min(pct, 100)) / 100)
    if w > 0:
        d.rectangle((7, 19, 7 + w, 45), fill=level_color(pct, charging))
    if charging:
        bolt = [(32, 6), (16, 34), (27, 34), (22, 58), (40, 28), (29, 28), (34, 6)]
        d.polygon(bolt, fill=BOLT, outline=BG)
    return img


class App:
    W, H = 280, 130

    def __init__(self, root, show_event):
        self.root = root
        root.title("Мышь")
        root.configure(bg=BG)
        root.resizable(False, False)
        if os.path.exists(ICON_FILE):
            root.iconbitmap(ICON_FILE)
        self.canvas = tk.Canvas(root, width=self.W, height=self.H, bg=BG, highlightthickness=0)
        self.canvas.pack(padx=24, pady=(24, 8))
        self.pct_label = tk.Label(root, bg=BG, fg=FG, font=("Segoe UI", 26, "bold"))
        self.pct_label.pack()
        self.info_label = tk.Label(root, bg=BG, fg=DIM, font=("Segoe UI", 11))
        self.info_label.pack(pady=(0, 20))

        self.mouse = RazerMouse()
        self.est = Estimator()
        self.state = None
        self.draw(None, False)
        self.pct_label.config(text="…")
        self.info_label.config(text="Ищу мышку")

        # крестик прячет окно в трей, выход — через меню иконки
        root.protocol("WM_DELETE_WINDOW", root.withdraw)
        self.tray = pystray.Icon(
            "MouseBattery", tray_image(None, False), "Мышь: ищу…",
            pystray.Menu(
                pystray.MenuItem("Открыть", lambda: root.after(0, self.show_window), default=True),
                pystray.MenuItem("Выход", lambda: root.after(0, self.quit)),
            ))
        self.tray.run_detached()

        # Все фоновые потоки отдают результат через root.after — tkinter сам
        # перекидывает вызов в главный поток, никаких таймеров-опросов в UI.
        self.devices_changed = threading.Event()
        self.watcher = DeviceWatcher(self.devices_changed)
        root.after(100, self.start_threads, show_event)

    def start_threads(self, show_event):
        threading.Thread(target=self.poll_loop, daemon=True).start()
        threading.Thread(target=self.wait_show, args=(show_event,), daemon=True).start()

    def wait_show(self, show_event):
        while True:   # спит в ядре, пока второй запуск не дёрнет событие
            kernel32.WaitForSingleObject(show_event, INFINITE)
            self.root.after(0, self.show_window)

    def poll_loop(self):
        rescan = True
        while True:
            try:
                res = self.mouse.read(rescan)
            except Exception:   # не роняем программу из-за HID
                self.mouse.close()
                res = None
            self.root.after(0, self.show, res)
            rescan = self.devices_changed.wait(POLL_SECONDS)
            if rescan:
                # при переключении провод/донгл событий прилетает пачка — ждём, пока утихнет
                while self.devices_changed.wait(1.5):
                    self.devices_changed.clear()
                self.devices_changed.clear()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit(self):
        self.est.save()
        self.tray.stop()
        self.root.destroy()

    def show(self, res):
        if res is None:
            pct = charging = None
            info = "Мышь не отвечает (спит или выключена)"
        else:
            pct, charging = res
            self.est.update(pct, charging)
            if charging:
                info = "Заряжается"
            else:
                hours, by_spec = self.est.remaining_hours(pct)
                suffix = " (по паспорту)" if by_spec else ""
                info = f"Осталось ≈ {fmt_hours(hours)}{suffix}"
        state = (pct, charging, info)
        if state == self.state:      # ничего не поменялось — ничего не перерисовываем
            return
        self.state = state
        self.draw(pct, charging)
        self.pct_label.config(text="—" if pct is None else f"{pct}%")
        self.info_label.config(text=info)
        self.tray.icon = tray_image(pct, charging)
        self.tray.title = ("Мышь не отвечает" if pct is None else f"Мышь: {pct}% · {info}")[:127]

    def draw(self, pct, charging):
        c = self.canvas
        c.delete("all")
        x0, y0, x1, y1 = 4, 10, self.W - 26, self.H - 10
        # корпус и «пипка»
        c.create_rectangle(x0, y0, x1, y1, outline=FG, width=4)
        c.create_rectangle(x1 + 2, y0 + 32, x1 + 18, y1 - 32, fill=FG, outline=FG)
        if pct is None:
            return
        pad = 9
        inner_w = (x1 - x0) - pad * 2
        fill_w = inner_w * max(0, min(pct, 100)) / 100
        if fill_w > 0:
            c.create_rectangle(x0 + pad, y0 + pad, x0 + pad + fill_w, y1 - pad,
                               fill=level_color(pct, charging), outline="")
        if charging:
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            bolt = [(6, -40), (-22, 6), (-3, 6), (-10, 40), (22, -8), (3, -8), (10, -40)]
            pts = [v for dx, dy in bolt for v in (cx + dx, cy + dy)]
            c.create_polygon(pts, fill=BOLT, outline=BG, width=3)


def main():
    mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        # уже запущены — просим первый экземпляр показать окно и выходим
        ev = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, SHOW_EVENT_NAME)
        if ev:
            kernel32.SetEvent(ev)
        return
    show_event = kernel32.CreateEventW(None, False, False, SHOW_EVENT_NAME)
    root = tk.Tk()
    App(root, show_event)
    root.mainloop()
    kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    main()
