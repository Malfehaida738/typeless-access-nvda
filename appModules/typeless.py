# Typeless Access - NVDA add-on
# Copyright (C) 2026 Mohammed Alfehaida
# This file is covered by the GNU General Public License, version 2 or later.
# See the file LICENSE for more details.

# -*- coding: utf-8 -*-
# Typeless Access - app module for the Typeless main window.
# Adds: a list of all controls with clear names (NVDA+Alt+E),
# and Alt+1..Alt+7 to jump to the main pages.

import re
import wx
import gui
import api
import ui
import core
import controlTypes
import textInfos
import appModuleHandler
from scriptHandler import script
from logHandler import log
from NVDAObjects import NVDAObject

CATEGORY = "Typeless"
HELP_NAME = "Explore features, Help center, and Release notes"
TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s?(AM|PM)$", re.IGNORECASE)

Role = controlTypes.Role
State = controlTypes.State
CONTROL_ROLES = {
	Role.BUTTON, Role.CHECKBOX, Role.COMBOBOX, Role.LINK, Role.TAB,
	Role.MENUITEM, Role.RADIOBUTTON, Role.TOGGLEBUTTON, Role.EDITABLETEXT,
	Role.SLIDER,
}
ROLE_WORDS = {
	Role.BUTTON: "زر",
	Role.CHECKBOX: "خانة اختيار",
	Role.COMBOBOX: "قائمة منسدلة",
	Role.LINK: "رابط",
	Role.TAB: "تبويب",
	Role.MENUITEM: "عنصر قائمة",
	Role.RADIOBUTTON: "زر اختيار",
	Role.TOGGLEBUTTON: "زر تبديل",
	Role.EDITABLETEXT: "حقل نص",
	Role.SLIDER: "منزلق",
}
SIDEBAR_ICONS = [
	("account", "الحساب (Account)"),
	("contact", "تواصل معنا (Contact us)"),
	("settings", "الإعدادات (Settings)"),
]


def _shorten(text, words=7):
	parts = text.split()
	if len(parts) <= words:
		return text
	return " ".join(parts[:words]) + "..."


def _title(context):
	"""Pick the setting title from the texts before a control:
	the last short text that is not a description sentence."""
	titles = [t for t in context if not t.endswith(".")]
	if titles:
		return titles[-1]
	return context[0] if context else ""


class _Collector(object):
	"""Walks the Typeless window and builds a list of controls with readable labels."""

	def __init__(self):
		self.items = []  # dicts: label, obj, key
		self.group = []  # texts seen since the previous control
		self.lastText = ""
		self.runIndex = 0
		self.runContext = []
		self.runFirstName = ""
		self.count = 0

	def walk(self, obj, depth=0):
		if obj is None or depth > 60 or self.count > 5000:
			return
		self.count += 1
		try:
			role = obj.role
			name = (obj.name or "").strip()
		except Exception:
			return
		if role in CONTROL_ROLES:
			self._addControl(obj, role, name)
			return
		if role == Role.STATICTEXT and name == "Check for updates":
			self.items.append({"label": "البحث عن تحديثات (Check for updates)", "obj": obj, "key": "updates", "role": Role.BUTTON})
			return
		if role == Role.STATICTEXT and name:
			self.group.append(name)
			self.lastText = name
		try:
			child = obj.firstChild
		except Exception:
			child = None
		while child is not None:
			self.walk(child, depth + 1)
			try:
				child = child.next
			except Exception:
				child = None

	def _addControl(self, obj, role, name):
		# Track runs of controls that follow the same group of texts (e.g. the 3 buttons of a history row).
		if self.group:
			self.runContext = list(self.group)
			self.runIndex = 0
			self.runFirstName = name
		else:
			self.runIndex += 1
		context = self.runContext
		label, key = self._label(obj, role, name, context)
		self.items.append({"label": label, "obj": obj, "key": key, "role": role})
		self.group = []

	def _label(self, obj, role, name, context):
		key = None
		if name:
			label = name
			if role == Role.BUTTON:
				key = {"Home": "home", "History": "history", "Dictionary": "dictionary", HELP_NAME: "help"}.get(name)
				if name == HELP_NAME:
					label = "المساعدة وميزات البرنامج وملاحظات الإصدار (Help)"
				elif name == "Add another" and context:
					label = "Add another - " + context[0]
			return label, key
		# Unnamed controls
		if role == Role.BUTTON:
			sidebar = self._sidebarIcon(obj)
			if sidebar:
				return sidebar[1], sidebar[0]
			first = context[0] if context else ""
			times = [t for t in context if TIME_RE.match(t)]
			if times:
				lastTime = max(i for i, t in enumerate(context) if TIME_RE.match(t))
				body = context[lastTime + 1:]
				about = context[lastTime] + ": " + _shorten(body[0]) if body else context[lastTime]
				if self.runFirstName == "Retry":
					names = ["Retry", "الإبلاغ عن مشكلة", "خيارات إضافية"]
				else:
					names = ["نسخ", "الإبلاغ عن مشكلة", "خيارات إضافية"]
				idx = self.runIndex if self.runIndex < len(names) else len(names) - 1
				return names[idx] + " - " + about, None
			if any(t.startswith("Press to start and stop") for t in context):
				return "حذف هذا الاختصار - " + first, None
			if first == "History":
				names = ["السحابة (غير مؤكد)", "خيارات إضافية للسجل"]
				idx = self.runIndex if self.runIndex < len(names) else len(names) - 1
				return names[idx], None
			if first:
				return "زر بلا اسم بعد: " + _shorten(first), None
			if self.lastText:
				return "زر بلا اسم بعد: " + _shorten(self.lastText), None
			return "زر بلا اسم", None
		title = _title(context)
		if title:
			return title, None
		if self.lastText:
			return self.lastText, None
		return "بلا اسم", None

	def _sidebarIcon(self, obj):
		return _sidebarIcon(obj)


def _rawName(obj):
	"""The name Typeless itself gives, without our overlay (avoids recursion)."""
	try:
		return (obj.IAccessibleObject.accName(obj.IAccessibleChildID) or "").strip()
	except Exception:
		pass
	try:
		return (obj.name or "").strip()
	except Exception:
		return ""


_sidebarCache = {}
_helpCache = {}
_diag = [0]


def _diagLog(msg):
	"""A few info lines in the NVDA log to help diagnose problems."""
	if _diag[0] < 40:
		_diag[0] += 1
		log.info("TypelessAccess: " + msg)


def _findHelpButton(root):
	"""Find the Help button (the one with the long name) under root, cached per window."""
	try:
		hwnd = root.windowHandle
	except Exception:
		hwnd = None
	cached = _helpCache.get(hwnd)
	if cached is not None:
		try:
			if cached.location:
				return cached
		except Exception:
			pass
	stack = [root]
	count = 0
	while stack and count < 1500:
		node = stack.pop()
		count += 1
		try:
			if node.role == Role.BUTTON and _rawName(node) == HELP_NAME:
				_helpCache[hwnd] = node
				return node
			kids = []
			child = node.firstChild
			while child is not None and len(kids) < 200:
				kids.append(child)
				child = child.next
			stack.extend(reversed(kids))
		except Exception:
			continue
	return None


CHART_ARROWS = [
	("chartPrev", "الأشهر السابقة في مخطط النشاط"),
	("chartNext", "الأشهر التالية في مخطط النشاط"),
]


def _chartArrow(obj, root):
	"""The two tiny arrows under the activity chart: left = previous, right = next."""
	try:
		l, t, w, h = obj.location
	except Exception:
		return None
	if not (0 < w <= 16 and 0 < h <= 16 and abs(w - h) <= 3):
		return None
	center = None
	try:
		rl, rt, rw, rh = root.location
		center = rl + rw / 2.0
	except Exception:
		center = None
	if center is None:
		return None
	return CHART_ARROWS[0] if (l + w / 2.0) < center else CHART_ARROWS[1]


def _iconByGeometry(obj, root):
	"""Classify an unnamed icon: chart arrows, or by its distance to the left of the Help button."""
	if root is not None:
		arrow = _chartArrow(obj, root)
		if arrow:
			return arrow
	helpBtn = _findHelpButton(root) if root is not None else None
	if helpBtn is None:
		_diagLog("help button not found")
		return None
	try:
		hl, ht, hw, hh = helpBtn.location
		l, t, w, h = obj.location
	except Exception:
		return None
	if hw <= 0 or abs(t - ht) > max(8, hh // 3) or l >= hl:
		return None
	steps = float(hl - l) / hw
	if steps < 1.5:
		return SIDEBAR_ICONS[2]
	if steps < 2.5:
		return SIDEBAR_ICONS[1]
	if steps < 8:
		return SIDEBAR_ICONS[0]
	return None


def _sidebarIcon(obj, root=None):
	"""For the 3 unnamed icons beside the Help button: returns (key, label) or None."""
	try:
		loc = obj.location
		cacheKey = (obj.windowHandle, tuple(loc) if loc else None)
	except Exception:
		cacheKey = None
	if cacheKey is not None and cacheKey in _sidebarCache:
		return _sidebarCache[cacheKey]
	result = None
	try:
		parent = obj.parent
		siblings = []
		hasHelp = False
		child = parent.firstChild
		n = 0
		while child is not None and n < 30:
			n += 1
			if child.role == Role.BUTTON:
				name = _rawName(child)
				if name == HELP_NAME:
					hasHelp = True
				elif not name:
					siblings.append(child)
			child = child.next
		if hasHelp and siblings:
			siblings.sort(key=lambda o: (o.location[0] if o.location else 0))
			for i, s in enumerate(siblings):
				if s == obj and i < len(SIDEBAR_ICONS):
					result = SIDEBAR_ICONS[i]
					break
	except Exception:
		result = None
	if result is None:
		if root is None:
			try:
				root = api.getForegroundObject()
			except Exception:
				root = None
		try:
			result = _iconByGeometry(obj, root)
		except Exception:
			result = None
	_diagLog("unnamed button at %s -> %s" % (cacheKey[1] if cacheKey else None, result[1] if result else None))
	if cacheKey is not None and cacheKey[1] is not None and len(_sidebarCache) < 500:
		_sidebarCache[cacheKey] = result
	return result


# ---------- Names in browse mode and object navigation ----------

class SidebarIconButton(NVDAObject):
	"""Gives the 3 unnamed sidebar icons a name (object navigation / focus)."""

	def _get_name(self):
		found = _sidebarIcon(self)
		if found:
			return found[1]
		return super().name


_TypelessVBuf = None
try:
	from NVDAObjects.IAccessible.chromium import ChromeVBuf as _BaseVBuf

	import textInfos as _textInfos

	class _TypelessVBufTextInfo(_BaseVBuf.TextInfo):
		"""Gives the unnamed sidebar icons a label in browse mode.
		NVDA reads a button's text content in browse mode, not its name, so we add the label as text."""

		def _normalizeControlField(self, attrs):
			attrs = super()._normalizeControlField(attrs)
			try:
				if attrs.get("role") == Role.BUTTON and not (attrs.get("name") or "").strip():
					docHandle = int(attrs.get("controlIdentifier_docHandle"))
					ID = int(attrs.get("controlIdentifier_ID"))
					buf = self.obj
					obj = buf.getNVDAObjectFromIdentifier(docHandle, ID)
					root = getattr(buf, "rootNVDAObject", None)
					found = _sidebarIcon(obj, root) if obj else None
					if found:
						attrs["_typelessLabel"] = found[1]
						_diagLog("browse label %s" % found[1])
			except Exception:
				log.debugWarning("TypelessAccess normalize", exc_info=True)
			return attrs

		def getTextWithFields(self, formatConfig=None):
			items = super().getTextWithFields(formatConfig)
			out = []
			n = len(items)
			for i, item in enumerate(items):
				out.append(item)
				try:
					if not (
						isinstance(item, _textInfos.FieldCommand)
						and item.command == "controlStart"
						and item.field is not None
						and item.field.get("_typelessLabel")
					):
						continue
					depth = 1
					hasText = False
					j = i + 1
					while j < n and depth > 0:
						it = items[j]
						if isinstance(it, str):
							if it.strip():
								hasText = True
								break
						elif isinstance(it, _textInfos.FieldCommand):
							if it.command == "controlStart":
								depth += 1
							elif it.command == "controlEnd":
								depth -= 1
						j += 1
					if not hasText:
						out.append(item.field.get("_typelessLabel"))
				except Exception:
					pass
			return out

	class _TypelessVBufClass(_BaseVBuf):
		"""Chromium browse mode buffer that labels the unnamed sidebar icons."""

		TextInfo = _TypelessVBufTextInfo

	_TypelessVBuf = _TypelessVBufClass
except Exception:
	log.warning("TypelessAccess: browse mode labels unavailable", exc_info=True)


class TypelessDocument(NVDAObject):
	"""Makes the Typeless document use our browse mode buffer."""

	def _get_treeInterceptorClass(self):
		if _TypelessVBuf is not None:
			_diagLog("browse mode buffer requested")
			return _TypelessVBuf
		return super().treeInterceptorClass


def _describe(item):
	obj = item["obj"]
	role = item["role"]
	text = item["label"]
	word = ROLE_WORDS.get(role, "")
	try:
		states = obj.states
	except Exception:
		states = set()
	if role == Role.CHECKBOX:
		word += " محددة" if State.CHECKED in states else " غير محددة"
	elif role == Role.COMBOBOX:
		try:
			value = obj.value
		except Exception:
			value = None
		if value:
			text += ": " + value
	if State.UNAVAILABLE in states:
		word += " معطل"
	return text + "، " + word


class ElementsDialog(wx.Dialog):

	def __init__(self, parent, items):
		super().__init__(parent, title="عناصر Typeless")
		self.items = items
		sizer = wx.BoxSizer(wx.VERTICAL)
		label = wx.StaticText(self, label="العناصر:")
		sizer.Add(label, 0, wx.ALL, 6)
		self.listBox = wx.ListBox(self, choices=[_describe(i) for i in items], style=wx.LB_SINGLE)
		if items:
			self.listBox.SetSelection(0)
		sizer.Add(self.listBox, 1, wx.EXPAND | wx.ALL, 6)
		buttons = wx.BoxSizer(wx.HORIZONTAL)
		activate = wx.Button(self, wx.ID_OK, "تفعيل")
		close = wx.Button(self, wx.ID_CANCEL, "إغلاق")
		buttons.Add(activate, 0, wx.ALL, 6)
		buttons.Add(close, 0, wx.ALL, 6)
		sizer.Add(buttons, 0)
		activate.SetDefault()
		self.listBox.Bind(wx.EVT_LISTBOX_DCLICK, lambda evt: self.EndModal(wx.ID_OK))
		self.SetSizer(sizer)
		self.SetSize((560, 460))
		self.listBox.SetFocus()


def _activate(item):
	obj = item["obj"]
	try:
		if item.get("key") == "updates":
			target = obj
			for attempt in range(4):
				try:
					target.doAction()
					break
				except Exception:
					target = target.parent
					if target is None:
						raise
		else:
			obj.doAction()
	except Exception:
		log.debugWarning("Typeless Access: doAction failed", exc_info=True)
		ui.message("تعذر تفعيل العنصر")
		return
	if item["role"] in (Role.CHECKBOX, Role.TOGGLEBUTTON):
		def report():
			try:
				ui.message("محددة" if State.CHECKED in obj.states else "غير محددة")
			except Exception:
				pass
		core.callLater(400, report)


STAT_LABELS = [
	("Words dictated", "الكلمات المُملاة"),
	("Time saved", "الوقت الموفَّر"),
	("Average dictation speed", "متوسط سرعة الإملاء"),
	("Total dictation time", "إجمالي وقت الإملاء"),
	("Active days", "أيام النشاط"),
	("Current streak", "السلسلة الحالية"),
	("Longest streak", "أطول سلسلة"),
	("Personalization", "نسبة التخصيص"),
]
UNIT_WORDS = {"words": "كلمة", "hr": "ساعة", "min": "دقيقة", "days": "يوم", "WPM": "كلمة في الدقيقة"}


def _allTexts(root, limit=3000):
	"""All static texts under root, in document order, with their objects."""
	out = []
	stack = [root]
	count = 0
	while stack and count < limit:
		node = stack.pop()
		count += 1
		try:
			role = node.role
			if role == Role.STATICTEXT:
				name = (node.name or "").strip()
				if name:
					out.append((name, node))
			kids = []
			child = node.firstChild
			while child is not None and len(kids) < 300:
				kids.append(child)
				child = child.next
			stack.extend(reversed(kids))
		except Exception:
			continue
	return out


def _statsSummary(root):
	texts = [t for t, o in _allTexts(root)]
	labels = dict(STAT_LABELS)
	parts = []
	value = []
	for t in texts:
		if t in labels:
			if value:
				spoken = " ".join(UNIT_WORDS.get(v, v) for v in value)
				parts.append(labels[t] + ": " + spoken)
			value = []
		elif t in UNIT_WORDS or re.match(r"^[\d.,]+\s?[%KkMm]?$", t):
			value.append(t)
	return parts


def _clickTextControl(root, text):
	"""Click a clickable text such as 'Check for updates' (tries the text, then its parents)."""
	for t, obj in _allTexts(root):
		if t == text:
			target = obj
			for attempt in range(4):
				if target is None:
					break
				try:
					target.doAction()
					return True
				except Exception:
					pass
				try:
					target = target.parent
				except Exception:
					target = None
			return False
	return None


_WEEKDAY_RE = re.compile(r"^[SMTWF]$")


def _cleanSkipped(text):
	"""Tidy the text skipped by Tab: drop empty lines and the chart's weekday letters."""
	lines = []
	for line in (text or "").splitlines():
		t = line.strip()
		if not t or _WEEKDAY_RE.match(t):
			continue
		lines.append(t)
	out = " ".join(lines)
	if len(out) > 1500:
		out = out[:1500]
	return out


class AppModule(appModuleHandler.AppModule):

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self._lastFocus = None
		self._tabDir = 0
		_diagLog("app module loaded, browse mode labels %s" % ("ready" if _TypelessVBuf is not None else "unavailable"))

	# ---------- Tab reads the text it skips ----------

	def _passTab(self, gesture, direction):
		self._tabDir = direction
		try:
			ti = api.getFocusObject().treeInterceptor
			if ti is not None and not ti.passThrough:
				func = ti.getScript(gesture)
				if func:
					func(gesture)
					return
		except Exception:
			log.debugWarning("TypelessAccess tab", exc_info=True)
		gesture.send()

	@script(gesture="kb:tab")
	def script_typelessTab(self, gesture):
		self._passTab(gesture, 1)

	@script(gesture="kb:shift+tab")
	def script_typelessShiftTab(self, gesture):
		self._passTab(gesture, -1)

	def _skippedText(self, obj):
		prev = self._lastFocus
		if prev is None or self._tabDir <= 0:
			return ""
		ti = obj.treeInterceptor
		if ti is None or prev.treeInterceptor is not ti:
			return ""
		newInfo = ti.makeTextInfo(obj)
		prevInfo = ti.makeTextInfo(prev)
		r = prevInfo.copy()
		r.collapse(end=True)
		if newInfo.compareEndPoints(prevInfo, "startToEnd") >= 0:
			r.setEndPoint(newInfo, "endToStart")
		else:
			# Tab wrapped to the top: read what was left at the end of the page.
			r.setEndPoint(ti.makeTextInfo(textInfos.POSITION_ALL), "endToEnd")
		return _cleanSkipped(r.text)

	def event_gainFocus(self, obj, nextHandler):
		try:
			if self._skipReading:
				text = self._skippedText(obj)
				if text:
					ui.message(text)
		except Exception:
			log.debugWarning("TypelessAccess skipped text", exc_info=True)
		self._lastFocus = obj
		self._tabDir = 0
		nextHandler()

	_skipReading = True

	@script(description="Typeless: تشغيل أو إيقاف قراءة النصوص التي يتخطاها Tab", gesture="kb:NVDA+alt+s", category=CATEGORY)
	def script_toggleSkipReading(self, gesture):
		self._skipReading = not self._skipReading
		ui.message("قراءة النصوص بين العناصر مفعلة" if self._skipReading else "قراءة النصوص بين العناصر متوقفة")

	def chooseNVDAObjectOverlayClasses(self, obj, clsList):
		try:
			role = obj.role
			if role == Role.DOCUMENT:
				clsList.insert(0, TypelessDocument)
			elif role == Role.BUTTON and not _rawName(obj):
				clsList.insert(0, SidebarIconButton)
		except Exception:
			log.debugWarning("TypelessAccess overlay", exc_info=True)

	def _collect(self):
		root = api.getForegroundObject()
		collector = _Collector()
		collector.walk(root)
		return collector.items

	def _goTo(self, key, spoken):
		try:
			items = self._collect()
		except Exception:
			log.debugWarning("Typeless Access: collect failed", exc_info=True)
			items = []
		for item in items:
			if item["key"] == key:
				try:
					item["obj"].doAction()
					ui.message(spoken)
				except Exception:
					ui.message("تعذر فتح " + spoken)
				return
		ui.message("لم أجد " + spoken + " في هذه الشاشة")

	@script(description="Typeless: قائمة بكل عناصر النافذة بأسماء واضحة", gesture="kb:NVDA+alt+e", category=CATEGORY)
	def script_elementsList(self, gesture):
		try:
			items = self._collect()
		except Exception:
			log.debugWarning("Typeless Access: collect failed", exc_info=True)
			ui.message("تعذر قراءة عناصر Typeless")
			return
		if not items:
			ui.message("لم أجد عناصر")
			return
		dialog = ElementsDialog(gui.mainFrame, items)

		def callback(result):
			if result != wx.ID_OK:
				return
			index = dialog.listBox.GetSelection()
			if index == wx.NOT_FOUND:
				return
			item = items[index]
			core.callLater(300, _activate, item)

		gui.runScriptModalDialog(dialog, callback)

	@script(description="Typeless: الصفحة الرئيسية", gesture="kb:alt+1", category=CATEGORY)
	def script_goHome(self, gesture):
		self._goTo("home", "الرئيسية")

	@script(description="Typeless: صفحة السجل", gesture="kb:alt+2", category=CATEGORY)
	def script_goHistory(self, gesture):
		self._goTo("history", "السجل")

	@script(description="Typeless: صفحة القاموس", gesture="kb:alt+3", category=CATEGORY)
	def script_goDictionary(self, gesture):
		self._goTo("dictionary", "القاموس")

	@script(description="Typeless: الإعدادات", gesture="kb:alt+4", category=CATEGORY)
	def script_goSettings(self, gesture):
		self._goTo("settings", "الإعدادات")

	@script(description="Typeless: الحساب", gesture="kb:alt+5", category=CATEGORY)
	def script_goAccount(self, gesture):
		self._goTo("account", "الحساب")

	@script(description="Typeless: تواصل معنا", gesture="kb:alt+6", category=CATEGORY)
	def script_goContact(self, gesture):
		self._goTo("contact", "تواصل معنا")

	@script(description="Typeless: المساعدة", gesture="kb:alt+7", category=CATEGORY)
	def script_goHelp(self, gesture):
		self._goTo("help", "المساعدة")

	@script(description="Typeless: البحث عن تحديثات (Check for updates)", gesture="kb:alt+8", category=CATEGORY)
	def script_checkUpdates(self, gesture):
		try:
			done = _clickTextControl(api.getForegroundObject(), "Check for updates")
		except Exception:
			done = False
		if done:
			ui.message("جارٍ البحث عن تحديثات")
		elif done is None:
			ui.message("لم أجد Check for updates. افتح الصفحة الرئيسية أولاً بـ Alt مع 1")
		else:
			ui.message("تعذر الضغط على Check for updates")

	@script(description="Typeless: قراءة إحصاءاتك والسلاسل (Streaks)", gesture="kb:alt+9", category=CATEGORY)
	def script_readStats(self, gesture):
		try:
			parts = _statsSummary(api.getForegroundObject())
		except Exception:
			log.debugWarning("TypelessAccess stats", exc_info=True)
			parts = []
		if parts:
			ui.message("، ".join(parts))
		else:
			ui.message("لم أجد الإحصاءات. افتح الصفحة الرئيسية أولاً بـ Alt مع 1")
