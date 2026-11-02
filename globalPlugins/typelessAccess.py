# -*- coding: utf-8 -*-
# Typeless Access - global part.
# Watches the Typeless microphone (Windows privacy registry) to announce dictation start/stop,
# reads the floating "Status" window, and keeps the last transcript / answer.

import time
import threading
import ctypes
import wx
from ctypes import wintypes
import winreg

import globalPluginHandler
import appModuleHandler
import api
import ui
import tones
import core
import controlTypes
from scriptHandler import script
from logHandler import log

try:
	from NVDAObjects.IAccessible import getNVDAObjectFromEvent
except Exception:
	getNVDAObjectFromEvent = None

CATEGORY = "Typeless"
POLL_MS = 300
OBJID_CLIENT = -4
MIC_PATH = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\NonPackaged"
AFTER_STOP_SECONDS = 30
RIPE_POLLS = 3

# Words Typeless shows that we translate instead of reading as is.
KNOWN = {
	"Thinking": "يعالج",
	"Ask anything": None,
	"Copy": None,
	"\"": None,
	"Copy last transcript": None,
}

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]
_user32.EnumWindows.restype = wintypes.BOOL
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowExW.restype = wintypes.HWND
_user32.GetClipboardSequenceNumber.argtypes = []
_user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.argtypes = []
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
_user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = wintypes.HANDLE
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = wintypes.LPVOID
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
CF_UNICODETEXT = 13
CAPTURE_SECONDS = 20


def _readClipboardText():
	"""Read the clipboard text quickly; returns None if busy or not text."""
	for attempt in range(5):
		if _user32.OpenClipboard(None):
			break
		time.sleep(0.005)
	else:
		return None
	try:
		if not _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
			return None
		handle = _user32.GetClipboardData(CF_UNICODETEXT)
		if not handle:
			return None
		ptr = _kernel32.GlobalLock(handle)
		if not ptr:
			return None
		try:
			return ctypes.wstring_at(ptr)
		finally:
			_kernel32.GlobalUnlock(handle)
	finally:
		_user32.CloseClipboard()


class _ClipboardCatcher(threading.Thread):
	"""After dictation stops, Typeless puts the text on the clipboard and pastes it.
	This thread catches the first new clipboard text in the next seconds."""

	def __init__(self, onText):
		super().__init__(daemon=True)
		self.onText = onText
		self.stopFlag = False
		self.startSeq = _user32.GetClipboardSequenceNumber()
		self.before = _readClipboardText()

	def run(self):
		lastSeq = self.startSeq
		end = time.time() + CAPTURE_SECONDS
		while not self.stopFlag and time.time() < end:
			seq = _user32.GetClipboardSequenceNumber()
			if seq != lastSeq:
				lastSeq = seq
				text = _readClipboardText()
				if text and text.strip() and text != self.before:
					if not self.stopFlag:
						wx.CallAfter(self.onText, text.strip())
					return
			time.sleep(0.01)


def _windowText(hwnd):
	buf = ctypes.create_unicode_buffer(256)
	_user32.GetWindowTextW(hwnd, buf, 256)
	return buf.value


def _isTypelessWindow(hwnd):
	pid = wintypes.DWORD()
	_user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
	try:
		name = appModuleHandler.getAppNameFromProcessID(pid.value)
	except Exception:
		return False
	return bool(name) and name.lower().startswith("typeless")


def _findTypelessWindow(title):
	found = []

	def callback(hwnd, lParam):
		try:
			if _windowText(hwnd) == title and _isTypelessWindow(hwnd):
				found.append(hwnd)
				return False
		except Exception:
			pass
		return True

	_user32.EnumWindows(_EnumWindowsProc(callback), 0)
	return found[0] if found else None


def _typelessMicOn(cache):
	"""True if Typeless is using the microphone now, False if not, None if unknown."""
	try:
		with winreg.OpenKey(winreg.HKEY_CURRENT_USER, MIC_PATH) as root:
			names = []
			if cache.get("key"):
				names.append(cache["key"])
			else:
				i = 0
				while True:
					try:
						sub = winreg.EnumKey(root, i)
					except OSError:
						break
					i += 1
					if "typeless" in sub.lower():
						names.append(sub)
			if not names:
				return None
			result = False
			for sub in names:
				try:
					with winreg.OpenKey(root, sub) as k:
						stop = winreg.QueryValueEx(k, "LastUsedTimeStop")[0]
						start = winreg.QueryValueEx(k, "LastUsedTimeStart")[0]
				except OSError:
					cache.pop("key", None)
					continue
				cache["key"] = sub
				if stop == 0 and start > 0:
					result = True
			return result
	except OSError:
		return None


def _collectTexts(obj, out, depth=0, counter=None):
	if counter is None:
		counter = [0]
	if obj is None or depth > 40 or counter[0] > 600:
		return
	counter[0] += 1
	try:
		role = obj.role
		name = obj.name
	except Exception:
		return
	if role in (controlTypes.Role.STATICTEXT, controlTypes.Role.TOOLTIP) and name:
		t = name.strip()
		if t and t not in out:
			out.append(t)
	try:
		child = obj.firstChild
	except Exception:
		child = None
	while child is not None:
		_collectTexts(child, out, depth + 1, counter)
		try:
			child = child.next
		except Exception:
			child = None


class GlobalPlugin(globalPluginHandler.GlobalPlugin):

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self._running = True
		self._micOn = None
		self._micCache = {}
		self._lastOff = 0.0
		self._tick = 0
		self._seen = set()
		self._pending = {}
		self._lastText = ""
		self._statusHwnd = None
		self._catcher = None
		self._autoRead = True
		core.callLater(2000, self._poll)

	def terminate(self):
		self._running = False
		self._stopCatcher()
		super().terminate()

	# ---------- Status window ----------

	def _statusTexts(self):
		if getNVDAObjectFromEvent is None:
			return []
		hwnd = self._statusHwnd
		if not hwnd or not _user32.IsWindow(hwnd):
			hwnd = _findTypelessWindow("Status")
			self._statusHwnd = hwnd
		if not hwnd:
			return []
		child = _user32.FindWindowExW(hwnd, None, "Chrome_RenderWidgetHostHWND", None)
		target = child or hwnd
		try:
			obj = getNVDAObjectFromEvent(target, OBJID_CLIENT, 0)
		except Exception:
			return []
		out = []
		_collectTexts(obj, out)
		return out

	# ---------- polling ----------

	def _poll(self):
		if not self._running:
			return
		try:
			on = _typelessMicOn(self._micCache)
			if on is not None and on != self._micOn:
				if self._micOn is not None:
					self._onMicChange(on)
				self._micOn = on
			if self._micOn or (time.time() - self._lastOff) < AFTER_STOP_SECONDS:
				self._tick += 1
				if self._tick % 2 == 0:
					self._checkStatus()
		except Exception:
			log.debugWarning("Typeless Access poll error", exc_info=True)
		core.callLater(POLL_MS, self._poll)

	def _stopCatcher(self):
		if self._catcher is not None:
			self._catcher.stopFlag = True
			self._catcher = None

	def _startCatcher(self):
		self._stopCatcher()
		try:
			self._catcher = _ClipboardCatcher(self._onCaughtText)
			self._catcher.start()
		except Exception:
			log.debugWarning("Typeless Access: clipboard catcher failed", exc_info=True)
			self._catcher = None

	def _onCaughtText(self, text):
		self._lastText = text
		self._catcher = None
		if self._autoRead:
			ui.message(text)

	def _onMicChange(self, on):
		if on:
			self._stopCatcher()
			tones.beep(880, 70)
			self._seen = set(self._statusTexts())
			self._pending = {}
			core.callLater(700, self._announceMode)
		else:
			tones.beep(440, 70)
			ui.message("توقف")
			self._lastOff = time.time()
			self._startCatcher()

	def _announceMode(self):
		try:
			texts = self._statusTexts()
		except Exception:
			texts = []
		self._seen.update(texts)
		joined = " ".join(texts)
		if "Ask anything" in joined:
			ui.message("بدأ: اسأل أي شيء")
		elif "Translat" in joined:
			ui.message("بدأت الترجمة")
		else:
			ui.message("بدأ الإملاء")

	def _checkStatus(self):
		texts = self._statusTexts()
		self._pending = {t: c for t, c in self._pending.items() if t in texts}
		ripe = []
		for t in texts:
			if t in self._seen:
				continue
			c = self._pending.get(t, 0) + 1
			self._pending[t] = c
			if c >= RIPE_POLLS:
				ripe.append(t)
		if not ripe:
			return
		for t in ripe:
			self._seen.add(t)
			self._pending.pop(t, None)
		self._announceRipe(ripe, texts)

	def _announceRipe(self, ripe, allTexts):
		if any("Copy last transcript" in t for t in ripe):
			candidates = [
				t for t in allTexts
				if "Copy last transcript" not in t and t not in KNOWN
			]
			transcript = max(candidates, key=len) if candidates else ""
			for t in allTexts:
				self._seen.add(t)
			if transcript:
				self._lastText = transcript
				ui.message("ظهرت رسالة نسخ آخر نص: " + transcript + ". للنسخ اضغط NVDA مع Alt مع C")
			else:
				ui.message("ظهرت رسالة نسخ آخر نص")
			return
		spoken = []
		for t in ripe:
			if t in KNOWN:
				if KNOWN[t]:
					spoken.append(KNOWN[t])
				continue
			if any(t != o and t in o for o in ripe):
				continue
			spoken.append(t)
		if not spoken:
			return
		message = " ".join(spoken)
		longText = " ".join(s for s in spoken if s not in KNOWN.values())
		if longText:
			self._lastText = longText
		ui.message(message)

	# ---------- scripts ----------

	@script(
		description="Typeless: الإعلان عن حالة الإملاء وما في الشريط العائم",
		gesture="kb:NVDA+alt+t",
		category=CATEGORY,
	)
	def script_reportStatus(self, gesture):
		on = _typelessMicOn(self._micCache)
		if on is None:
			state = "لم أجد Typeless"
		elif on:
			state = "Typeless يستمع الآن"
		else:
			state = "Typeless متوقف"
		texts = [t for t in self._statusTexts() if t not in KNOWN or KNOWN.get(t)]
		texts = [KNOWN.get(t) or t for t in texts]
		if texts and on:
			ui.message(state + ". " + " ".join(texts))
		else:
			ui.message(state)

	@script(
		description="Typeless: قراءة آخر نص محفوظ (آخر نص مُملى أو آخر جواب)",
		gesture="kb:NVDA+alt+l",
		category=CATEGORY,
	)
	def script_readLast(self, gesture):
		if self._lastText:
			ui.message(self._lastText)
		else:
			ui.message("لا يوجد نص محفوظ بعد")

	@script(
		description="Typeless: تشغيل أو إيقاف القراءة التلقائية للنص بعد الإملاء",
		gesture="kb:NVDA+alt+r",
		category=CATEGORY,
	)
	def script_toggleAutoRead(self, gesture):
		self._autoRead = not self._autoRead
		ui.message("القراءة التلقائية مفعلة" if self._autoRead else "القراءة التلقائية متوقفة")

	@script(
		description="Typeless: نسخ آخر نص محفوظ إلى الحافظة",
		gesture="kb:NVDA+alt+c",
		category=CATEGORY,
	)
	def script_copyLast(self, gesture):
		if not self._lastText:
			ui.message("لا يوجد نص محفوظ بعد")
			return
		try:
			api.copyToClip(self._lastText)
			ui.message("نُسخ آخر نص")
		except Exception:
			ui.message("تعذر النسخ")

	# ---------- Stats from anywhere ----------

	def _mainRoot(self):
		if getNVDAObjectFromEvent is None:
			return None
		hwnd = _findTypelessWindow("Typeless")
		if not hwnd:
			return None
		child = _user32.FindWindowExW(hwnd, None, "Chrome_RenderWidgetHostHWND", None)
		try:
			return getNVDAObjectFromEvent(child or hwnd, OBJID_CLIENT, 0)
		except Exception:
			return None

	def _statsNow(self):
		import appModules.typeless as tl
		root = self._mainRoot()
		if root is None:
			return None, None
		return tl, tl._statsSummary(root)

	@script(
		description="Typeless: قراءة الإحصاءات من أي مكان بدون فتح التطبيق",
		gesture="kb:NVDA+alt+9",
		category=CATEGORY,
	)
	def script_statsAnywhere(self, gesture):
		try:
			tl, parts = self._statsNow()
		except Exception:
			log.debugWarning("TypelessAccess stats anywhere", exc_info=True)
			ui.message("تعذرت قراءة الإحصاءات")
			return
		if tl is None:
			ui.message("نافذة Typeless الرئيسية غير مفتوحة. افتحها ثم أعد المحاولة")
			return
		if parts:
			ui.message("، ".join(parts))
			return
		# Not on the Home page: open it in the background, then try again.
		try:
			collector = tl._Collector()
			collector.walk(self._mainRoot())
			for item in collector.items:
				if item["key"] == "home":
					item["obj"].doAction()
					break
		except Exception:
			log.debugWarning("TypelessAccess open home", exc_info=True)
			ui.message("لم أجد الإحصاءات. افتح الصفحة الرئيسية")
			return
		ui.message("أفتح الصفحة الرئيسية")

		def retry():
			try:
				tl2, parts2 = self._statsNow()
			except Exception:
				parts2 = []
			ui.message("، ".join(parts2) if parts2 else "لم أجد الإحصاءات")

		core.callLater(1500, retry)
