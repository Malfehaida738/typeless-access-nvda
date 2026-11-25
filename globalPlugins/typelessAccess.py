# Typeless Access - NVDA add-on
# Copyright (C) 2026 Mohammed Alfehaida
# This file is covered by the GNU General Public License, version 2 or later.
# See the file LICENSE for more details.

# Typeless Access - global part.
# Spots the quick right-Alt tap that starts and stops a Typeless dictation, announces it,
# reads the dictated text back (clipboard, with the focused field as a fallback),
# reads the floating "Status" window, and keeps the last transcript / answer.

import time
import threading
import ctypes
import wx
from ctypes import wintypes

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
AFTER_STOP_SECONDS = 30
RIPE_POLLS = 3
VK_RMENU = 0xA5  # the right Alt key
TAP_MAX_SECONDS = 1.0
TAP_DEBOUNCE_SECONDS = 0.35
# After this long without a second tap, ask the Typeless overlay whether it is still recording.
RECORDING_CHECK_SECONDS = 180
RECORDING_MAX_SECONDS = 1800
# How long the clipboard is watched for the dictated text after dictation stops.
CAPTURE_SECONDS = 90
# The focused field is compared again after these delays (ms) when the clipboard showed nothing.
FALLBACK_DELAYS = (1800, 2500, 4000, 6000)
# Reading the whole field is skipped when it proved slow (very large documents).
SLOW_FIELD_SECONDS = 0.25
PID_TTL_SECONDS = 30

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
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsWindowVisible.restype = wintypes.BOOL
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
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

# Returned by _readClipboardText when another program holds the clipboard open.
_BUSY = object()


def _readClipboardText():
	"""Read the clipboard text quickly.
	Returns the text, None when the clipboard holds no text, or _BUSY when it could not be opened."""
	for attempt in range(5):
		if _user32.OpenClipboard(None):
			break
		time.sleep(0.005)
	else:
		return _BUSY
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
	"""Typeless puts the dictated text on the clipboard and pastes it.
	This thread catches the first new clipboard text and hands it over once."""

	def __init__(self, onText, seconds):
		super().__init__(daemon=True)
		self.onText = onText
		self.stopFlag = False
		self.deadline = time.time() + seconds
		self.startSeq = _user32.GetClipboardSequenceNumber()
		before = _readClipboardText()
		self.before = None if before is _BUSY else before

	def run(self):
		lastSeq = self.startSeq
		while not self.stopFlag and time.time() < self.deadline:
			seq = _user32.GetClipboardSequenceNumber()
			if seq != lastSeq:
				text = _readClipboardText()
				if text is _BUSY:
					# Its writer still holds the clipboard: look again on the next round.
					time.sleep(0.01)
					continue
				lastSeq = seq
				if text and text.strip() and text != self.before:
					if not self.stopFlag:
						wx.CallAfter(self.onText, self, text.strip())
					return
			time.sleep(0.01)


def _windowText(hwnd):
	buf = ctypes.create_unicode_buffer(256)
	_user32.GetWindowTextW(hwnd, buf, 256)
	return buf.value


def _windowPid(hwnd):
	pid = wintypes.DWORD()
	_user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
	return pid.value


# pid -> (is it Typeless, when we asked). Windows reuses process ids, so answers expire.
_pidCache = {}


def _pidIsTypeless(pid):
	now = time.time()
	hit = _pidCache.get(pid)
	if hit is not None and 0 <= now - hit[1] < PID_TTL_SECONDS:
		return hit[0]
	try:
		name = appModuleHandler.getAppNameFromProcessID(pid)
	except Exception:
		name = ""
	ok = bool(name) and name.lower().startswith("typeless")
	if len(_pidCache) > 2000:
		_pidCache.clear()
	_pidCache[pid] = (ok, now)
	return ok


def _isTypelessWindow(hwnd):
	return _pidIsTypeless(_windowPid(hwnd))


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
	"""True while Typeless shows its large dictation overlay, False when it is the small idle icon,
	None if the Status window is absent."""
	hwnd = holder.get("hwnd")
	if not hwnd or not _user32.IsWindow(hwnd):
		hwnd = _findTypelessWindow("Status")
		holder["hwnd"] = hwnd
	if not hwnd:
		return None
	if not _user32.IsWindowVisible(hwnd):
		return False
	rect = wintypes.RECT()
	if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
		return None
	return (rect.right - rect.left) > 200


# One Typeless window and its process id, remembered so the check is cheap.
_tlWindow = [None, 0]


def _typelessAlive():
	"""Cheap check that Typeless is running: remember one of its windows and test it."""
	hwnd, pid = _tlWindow
	if hwnd and _user32.IsWindow(hwnd) and _windowPid(hwnd) == pid:
		return True
	_tlWindow[0] = None
	found = []

	def callback(hwnd, lParam):
		try:
			pid = _windowPid(hwnd)
			if _pidIsTypeless(pid):
				found.append((hwnd, pid))
				return False
		except Exception:
			pass
		return True

	_user32.EnumWindows(_EnumWindowsProc(callback), 0)
	if found:
		_tlWindow[0], _tlWindow[1] = found[0]
		return True
	return False


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


def _addedText(old, new):
	"""The part of new that is not in old (common start and end removed)."""
	p = 0
	limit = min(len(old), len(new))
	while p < limit and old[p] == new[p]:
		p += 1
	q = 0
	while q < limit - p and old[len(old) - 1 - q] == new[len(new) - 1 - q]:
		q += 1
	return new[p:len(new) - q].strip()


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
		self._recordingCheckAt = 0.0
		self._statusHolder = {}
		self._lastOff = 0.0
		self._tick = 0
		self._seen = set()
		self._pending = {}
		self._lastText = ""
		self._statusHwnd = None
		self._catcher = None
		self._autoRead = True
		# Each dictation gets a number, so late timers of an earlier dictation do nothing.
		self._session = 0
		self._caught = False
		self._startValue = None
		self._startObj = None
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

	# ---------- the right-Alt tap ----------

	def _watchTypeless(self):
		"""Keep a cheap flag up to date: is Typeless running? (done off the main thread)"""
		while self._running:
			try:
				up = _typelessAlive()
				# Typeless closed (an update restarts it): the start/stop state we remember
				# is no longer true, so forget it.
				if not up and self._typelessUp:
					wx.CallAfter(self._onTypelessRestart)
				self._typelessUp = up
			except Exception:
				pass
			time.sleep(1.0)

	def _onTypelessRestart(self):
		"""Forget the dictation state when Typeless restarts, so the next tap is read as a start."""
		if not self._running:
			return
		log.info("TypelessAccess: Typeless restarted or closed, dictation state reset")
		self._micOn = False
		self._session += 1
		self._stopCatcher()

	def _rawKey(self, vkCode=None, scanCode=None, extended=None, pressed=None, **kw):
		"""Typeless 2.11 starts and stops dictation with a quick tap of the right Alt key
		(it no longer updates the Windows microphone record). Spot a lone right-Alt tap.
		Runs on NVDA's keyboard hook thread, so it only notes the time and hands over."""
		try:
			isAlt = vkCode == VK_RMENU
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
				now = time.time()
				if down is not None and clean and (now - down) < TAP_MAX_SECONDS:
					wx.CallAfter(self._altTap, now)
		except Exception:
			pass
		return True

	def _altTap(self, eventTime=None):
		if not self._running:
			return
		if eventTime is None:
			eventTime = time.time()
		if not self._typelessUp:
			# The background check can be up to a second behind (right after Typeless starts):
			# ask again now instead of dropping the tap, which would flip start and stop.
			try:
				if _typelessAlive():
					self._typelessUp = True
			except Exception:
				pass
		if not self._typelessUp:
			log.info("TypelessAccess: right-Alt tap ignored, Typeless is not running")
			return
		# Debounce on the time of the key press itself, not on when we got round to handling it.
		if eventTime - self._lastTap < TAP_DEBOUNCE_SECONDS:
			log.info("TypelessAccess: right-Alt tap ignored (too close to the previous one)")
			return
		self._lastTap = eventTime
		before = self._micOn
		if self._micOn and eventTime - self._recordingSince > 2.5:
			# Typeless may have ended the dictation by itself (its overlay is closed again).
			try:
				if _statusExpanded(self._statusHolder) is False:
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
		description="Typeless: تشغيل أو إيقاف إعلان بدء الإملاء وتوقفه عند نقر Alt الأيمن",
		gesture="kb:NVDA+alt+a",
		category=CATEGORY,
	)
	def script_toggleTapAnnounce(self, gesture):
		self._tapAnnounce = not self._tapAnnounce
		ui.message("إعلان الإملاء مفعل" if self._tapAnnounce else "إعلان الإملاء متوقف")

	# ---------- polling ----------

	def _poll(self):
		if not self._running:
			return
		try:
			now = time.time()
			if self._micOn:
				catcher = self._catcher
				if catcher is not None:
					# Keep watching the clipboard for as long as the dictation lasts.
					catcher.deadline = now + CAPTURE_SECONDS
				if now >= self._recordingCheckAt:
					self._checkStillRecording(now)
			elif (now - self._lastOff) < AFTER_STOP_SECONDS:
				self._tick += 1
				if self._tick % 2 == 0:
					self._checkStatus()
		except Exception:
			log.debugWarning("Typeless Access poll error", exc_info=True)
		core.callLater(POLL_MS, self._poll)

	def _checkStillRecording(self, now):
		"""A long time without a second tap: a long dictation, or we missed the end of it?"""
		expanded = None
		try:
			expanded = _statusExpanded(self._statusHolder)
		except Exception:
			pass
		if expanded is True and now - self._recordingSince < RECORDING_MAX_SECONDS:
			# The overlay is still open: the user is still dictating. Look again later.
			self._recordingCheckAt = now + 5
			return
		# The dictation ended without us seeing it. Forget it quietly: there is nothing to announce.
		log.info("TypelessAccess: no second tap, dictation state reset")
		self._micOn = False
		self._stopCatcher()

	# ---------- catching the dictated text ----------

	def _stopCatcher(self):
		if self._catcher is not None:
			self._catcher.stopFlag = True
			self._catcher = None

	def _startCatcher(self):
		self._stopCatcher()
		try:
			self._catcher = _ClipboardCatcher(self._onCaughtText, CAPTURE_SECONDS)
			self._catcher.start()
		except Exception:
			log.debugWarning("Typeless Access: clipboard catcher failed", exc_info=True)
			self._catcher = None

	def _ensureCatcher(self):
		"""Make sure the clipboard is being watched for the next CAPTURE_SECONDS."""
		catcher = self._catcher
		if catcher is not None and catcher.is_alive() and not catcher.stopFlag:
			catcher.deadline = time.time() + CAPTURE_SECONDS
			if catcher.is_alive():
				return
		self._startCatcher()

	def _prepareCapture(self, session):
		"""The slow part of starting: watch the clipboard and remember the field text."""
		if not self._running or session != self._session or not self._micOn:
			return
		self._startCatcher()
		started = time.time()
		obj, value = self._focusField()
		if value is not None and time.time() - started > SLOW_FIELD_SECONDS:
			# Reading this field is slow: do not read it again and again after the dictation.
			log.info("TypelessAccess: focused field is slow to read, field fallback off for this dictation")
			obj, value = None, None
		if session == self._session:
			self._startObj = obj
			self._startValue = value

	def _focusField(self):
		"""The focused editable field and its whole text, or (None, None)."""
		try:
			obj = api.getFocusObject()
			if obj is None or controlTypes.State.EDITABLE not in obj.states:
				return None, None
			return obj, obj.makeTextInfo(textInfos.POSITION_ALL).text
		except Exception:
			return None, None

	def _fieldFallback(self, session, attempt=0):
		"""If the clipboard never showed the text, read what was added to the focused field."""
		if not self._running or session != self._session or self._caught:
			return
		old = self._startValue
		startObj = self._startObj
		if old is None or startObj is None:
			return
		obj, new = self._focusField()
		try:
			same = obj is not None and obj == startObj
		except Exception:
			same = False
		if not same or new is None:
			# The focus moved to another place: comparing the two texts would mean nothing.
			return
		added = _addedText(old, new) if new != old else ""
		if not added:
			if attempt < len(FALLBACK_DELAYS) - 1:
				core.callLater(FALLBACK_DELAYS[attempt + 1], self._fieldFallback, session, attempt + 1)
			return
		self._caught = True
		self._lastText = added
		self._stopCatcher()
		if self._autoRead:
			ui.message(added)

	def _onCaughtText(self, catcher, text):
		if not self._running:
			return
		if catcher is self._catcher:
			self._catcher = None
			self._caught = True
		# (Text from an older watcher is still worth reading, but it does not settle this dictation.)
		self._lastText = text
		if self._autoRead:
			ui.message(text)

	def _onMicChange(self, on):
		announce = self._tapAnnounce
		if on:
			self._session += 1
			self._caught = False
			self._startValue = None
			self._startObj = None
			self._seen = set()
			self._pending = {}
			self._recordingCheckAt = time.time() + RECORDING_CHECK_SECONDS
			if announce:
				tones.beep(880, 70)
				ui.message("بدأ الإملاء")
			core.callLater(60, self._prepareCapture, self._session)
		else:
			if announce:
				tones.beep(440, 70)
				ui.message("توقف")
			self._lastOff = time.time()
			self._ensureCatcher()
			core.callLater(FALLBACK_DELAYS[0], self._fieldFallback, self._session, 0)

	# ---------- texts of the Status window after dictation ----------

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
			catcher = self._catcher
			if catcher is not None:
				# Our own copy must not be taken for a new dictation.
				catcher.before = self._lastText
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
		opened = False
		try:
			collector = tl._Collector()
			collector.walk(self._mainRoot())
			for item in collector.items:
				if item["key"] == "home":
					item["obj"].doAction()
					opened = True
					break
		except Exception:
			log.debugWarning("TypelessAccess open home", exc_info=True)
		if not opened:
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
