# Typeless Access - NVDA add-on
# Copyright (C) 2026 Mohammed Alfehaida
# This file is covered by the GNU General Public License, version 2 or later.
# See the file LICENSE for more details.

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
import textInfos
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
CAPTURE_SECONDS = 90


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


def _statusExpanded(holder):
	"""True while Typeless shows its large dictation overlay (Typeless 2.11 no longer
	updates the Windows microphone record); False when it is the small idle icon; None if absent."""
	hwnd = holder.get("hwnd")
	if not hwnd or not _user32.IsWindow(hwnd):
		hwnd = _findTypelessWindow("Status")
		holder["hwnd"] = hwnd
	if not hwnd:
		return None
	if not _user32.IsWindowVisible(hwnd):
		return False
	rect = wintypes.RECT()
	_user32.GetWindowRect(hwnd, ctypes.byref(rect))
	return (rect.right - rect.left) > 200


def _registryMicOn(cache):
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


_pidCache = {}


def _typelessWindowsState():
	"""Title, visibility and size of every top-level Typeless window (to find a recording signal)."""
	rows = []

	def callback(hwnd, lParam):
		try:
			pid = wintypes.DWORD()
			_user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
			ok = _pidCache.get(pid.value)
			if ok is None:
				try:
					nm = appModuleHandler.getAppNameFromProcessID(pid.value)
				except Exception:
					nm = ""
				ok = bool(nm) and nm.lower().startswith("typeless")
				_pidCache[pid.value] = ok
			if ok:
				rect = wintypes.RECT()
				_user32.GetWindowRect(hwnd, ctypes.byref(rect))
				rows.append("%s|vis=%s|%sx%s@%s,%s" % (
					_windowText(hwnd) or "(no title)", bool(_user32.IsWindowVisible(hwnd)),
					rect.right - rect.left, rect.bottom - rect.top, rect.left, rect.top))
		except Exception:
			pass
		return True

	_user32.EnumWindows(_EnumWindowsProc(callback), 0)
	return tuple(sorted(rows))


_tlHwnd = [None]


def _typelessAlive():
	"""Cheap check that Typeless is running: remember one of its windows and test it."""
	h = _tlHwnd[0]
	if h and _user32.IsWindow(h):
		return True
	_tlHwnd[0] = None
	found = []

	def callback(hwnd, lParam):
		try:
			pid = wintypes.DWORD()
			_user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
			ok = _pidCache.get(pid.value)
			if ok is None:
				try:
					nm = appModuleHandler.getAppNameFromProcessID(pid.value)
				except Exception:
					nm = ""
				ok = bool(nm) and nm.lower().startswith("typeless")
				_pidCache[pid.value] = ok
			if ok:
				found.append(hwnd)
				return False
		except Exception:
			pass
		return True

	_user32.EnumWindows(_EnumWindowsProc(callback), 0)
	if found:
		_tlHwnd[0] = found[0]
		return True
	return False


def _activeMicKeys():
	"""Names of every app that is using the microphone right now (stop time 0)."""
	active = set()
	for path in (MIC_PATH, MIC_PATH.replace("NonPackaged", "")[:-1]):
		try:
			with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as root:
				i = 0
				while True:
					try:
						sub = winreg.EnumKey(root, i)
					except OSError:
						break
					i += 1
					try:
						with winreg.OpenKey(root, sub) as k:
							start = winreg.QueryValueEx(k, "LastUsedTimeStart")[0]
							stop = winreg.QueryValueEx(k, "LastUsedTimeStop")[0]
						if stop == 0 and start > 0:
							active.add(sub)
					except OSError:
						pass
		except OSError:
			pass
	return active


def _micDump():
	"""Write every microphone-consent entry to the NVDA log (to diagnose detection problems)."""
	lines = []
	for path in (MIC_PATH, MIC_PATH.replace("NonPackaged", "")[:-1]):
		try:
			with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as root:
				i = 0
				while True:
					try:
						sub = winreg.EnumKey(root, i)
					except OSError:
						break
					i += 1
					try:
						with winreg.OpenKey(root, sub) as k:
							start = winreg.QueryValueEx(k, "LastUsedTimeStart")[0]
							stop = winreg.QueryValueEx(k, "LastUsedTimeStop")[0]
						lines.append("%s | %s | start=%s stop=%s" % (path.split("\\")[-1], sub, start, stop))
					except OSError:
						lines.append("%s | %s | (no times)" % (path.split("\\")[-1], sub))
		except OSError:
			lines.append("cannot open " + path)
	log.info("TypelessAccess mic keys:\n" + "\n".join(lines))
	return len(lines)


def _typelessMicOn(cache):
	"""True while Typeless is listening: the overlay is open or the microphone record says so."""
	reg = None
	try:
		reg = _registryMicOn(cache)
	except Exception:
		pass
	stat = None
	try:
		stat = _statusExpanded(cache.setdefault("status", {}))
	except Exception:
		pass
	if reg is None and stat is None:
		return None
	return bool(reg) or bool(stat)


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
		self._micOn = False
		self._altDown = None
		self._altClean = True
		self._tapAnnounce = True
		self._typelessUp = False
		self._lastTap = 0.0
		self._recordingSince = 0.0
		self._micCache = {}
		self._lastOff = 0.0
		self._tick = 0
		self._seen = set()
		self._pending = {}
		self._lastText = ""
		self._statusHwnd = None
		self._catcher = None
		self._autoRead = True
		try:
			import inputCore
			inputCore.decide_handleRawKey.register(self._rawKey)
			log.info("TypelessAccess: Alt tap hook registered")
		except Exception:
			log.debugWarning("Typeless Access: raw key hook unavailable", exc_info=True)
		threading.Thread(target=self._watchTypeless, daemon=True).start()
		core.callLater(2000, self._poll)

	def terminate(self):
		self._running = False
		try:
			import inputCore
			inputCore.decide_handleRawKey.unregister(self._rawKey)
		except Exception:
			pass
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

	def _watchTypeless(self):
		"""Keep a cheap flag up to date: is Typeless running? (done off the main thread)"""
		while self._running:
			try:
				self._typelessUp = _typelessAlive()
			except Exception:
				pass
			time.sleep(2.0)

	def _rawKey(self, vkCode=None, scanCode=None, extended=None, pressed=None, **kw):
		"""Typeless 2.11 starts and stops dictation with a quick tap of the Alt key
		(it no longer updates the Windows microphone record). Spot a lone right-Alt tap."""
		try:
			isAlt = vkCode == 0xA5  # Typeless uses the right Alt key only
			if pressed:
				if isAlt:
					if self._altDown is None:
						self._altDown = time.time()
						self._altClean = True
				else:
					self._altClean = False
			elif isAlt:
				down = self._altDown
				clean = self._altClean
				self._altDown = None
				if down is not None and clean and (time.time() - down) < 1.0:
					wx.CallAfter(self._altTap, time.time())
		except Exception:
			pass
		return True

	def _altTap(self, eventTime=None):
		if not self._tapAnnounce or not self._running:
			return
		if eventTime is None:
			eventTime = time.time()
		if not self._typelessUp:
			log.info("TypelessAccess: right-Alt tap ignored, Typeless is not running")
			return
		# Debounce on the time of the key press itself, not on when we got round to handling it.
		if eventTime - self._lastTap < 0.35:
			log.info("TypelessAccess: right-Alt tap ignored (too close to the previous one)")
			return
		self._lastTap = eventTime
		before = self._micOn
		if self._micOn and eventTime - self._recordingSince > 2.5:
			# Typeless may have ended the dictation by itself (its overlay is closed again).
			try:
				if _statusExpanded(self._micCache.setdefault("status", {})) is False:
					self._micOn = False
					self._stopCatcher()
			except Exception:
				pass
		if not self._micOn:
			self._micOn = True
			self._recordingSince = eventTime
			log.info("TypelessAccess: right-Alt tap -> start (was %s)" % before)
			self._onMicChange(True)
		else:
			self._micOn = False
			log.info("TypelessAccess: right-Alt tap -> stop")
			self._onMicChange(False)

	@script(
		description="Typeless: تشغيل أو إيقاف إعلان بدء الإملاء وتوقفه عند نقر Alt",
		gesture="kb:NVDA+alt+a",
		category=CATEGORY,
	)
	def script_toggleTapAnnounce(self, gesture):
		self._tapAnnounce = not self._tapAnnounce
		self._micOn = False
		ui.message("إعلان الإملاء مفعل" if self._tapAnnounce else "إعلان الإملاء متوقف")

	def _poll(self):
		if not self._running:
			return
		try:
			if self._micOn and time.time() - self._recordingSince > 180:
				# Safety: we lost the second Alt tap; stop waiting for it.
				self._micOn = False
				self._onMicChange(False)
			if not self._micOn and (time.time() - self._lastOff) < AFTER_STOP_SECONDS:
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

	def _prepareCapture(self):
		"""The slow part of starting: remember the field text and start watching the clipboard."""
		if not self._micOn:
			return
		self._startValue = self._focusText()
		if self._micOn:
			self._startCatcher()

	def _focusText(self):
		try:
			obj = api.getFocusObject()
			if obj is None or controlTypes.State.EDITABLE not in obj.states:
				return None
			return obj.makeTextInfo(textInfos.POSITION_ALL).text
		except Exception:
			return None

	def _fieldFallback(self):
		"""If the clipboard never showed the text, read what was added to the focused field."""
		if getattr(self, "_caught", False):
			return
		old = getattr(self, "_startValue", None)
		new = self._focusText()
		if old is None or new is None or new == old:
			return
		p = 0
		limit = min(len(old), len(new))
		while p < limit and old[p] == new[p]:
			p += 1
		q = 0
		while q < limit - p and old[len(old) - 1 - q] == new[len(new) - 1 - q]:
			q += 1
		added = new[p:len(new) - q].strip()
		if added:
			self._caught = True
			self._lastText = added
			self._stopCatcher()
			if self._autoRead:
				ui.message(added)

	def _onCaughtText(self, text):
		self._caught = True
		self._lastText = text
		self._catcher = None
		if self._autoRead:
			ui.message(text)

	def _onMicChange(self, on):
		if on:
			self._caught = False
			self._startValue = None
			tones.beep(880, 70)
			ui.message("بدأ الإملاء")
			self._seen = set()
			self._pending = {}
			core.callLater(60, self._prepareCapture)
		else:
			tones.beep(440, 70)
			ui.message("توقف")
			self._lastOff = time.time()
			if not getattr(self, "_caught", False) and self._catcher is None:
				self._startCatcher()
			core.callLater(1800, self._fieldFallback)

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
		on = bool(self._micOn)
		if not _typelessAlive():
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
