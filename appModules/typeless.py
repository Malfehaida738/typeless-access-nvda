# Typeless Access - NVDA add-on
# Copyright (C) 2026 Mohammed Alfehaida
# This file is covered by the GNU General Public License, version 2 or later.
# See the file LICENSE for more details.

# Typeless Access - app module for the Typeless main window.
# Adds: a list of all controls with clear names (NVDA+Alt+E),
# Alt+1..Alt+9 to jump to the main pages and read the stats,
# and names for the sidebar icons that Typeless leaves unnamed.

import re
import time
import ctypes
from ctypes import wintypes
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
# On/off switches have their own role and state in recent NVDA versions.
ROLE_SWITCH = getattr(Role, "SWITCH", None)
STATE_ON = getattr(State, "ON", None)
STATE_PRESSED = getattr(State, "PRESSED", None)
OBJID_CLIENT = -4
CONTROL_ROLES = {
	Role.BUTTON, Role.CHECKBOX, Role.COMBOBOX, Role.LINK, Role.TAB,
	Role.MENUITEM, Role.RADIOBUTTON, Role.TOGGLEBUTTON, Role.EDITABLETEXT,
	Role.SLIDER,
}
if ROLE_SWITCH is not None:
	CONTROL_ROLES.add(ROLE_SWITCH)
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
if ROLE_SWITCH is not None:
	ROLE_WORDS[ROLE_SWITCH] = "مفتاح تبديل"
SIDEBAR_ICONS = [
	("account", "الحساب (Account)"),
	("contact", "تواصل معنا (Contact us)"),
	("settings", "الإعدادات (Settings)"),
]
HELP_ICON = ("help", "المساعدة (Help)")
# Button name -> the page it opens. Includes the names this add-on gives the unnamed icons.
NAME_KEYS = {
	"Home": "home",
	"History": "history",
	"Dictionary": "dictionary",
	HELP_NAME: "help",
	"Check for updates": "updates",
	HELP_ICON[1]: HELP_ICON[0],
}
NAME_KEYS.update({label: key for key, label in SIDEBAR_ICONS})

# Our own handle to user32, with full 64-bit window handles
# (the shared ctypes.windll.user32 has no declared types and cuts them to 32 bits).
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowExW.restype = wintypes.HWND
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = wintypes.HWND


def _typelessRoot():
	"""The web document of the Typeless window. The top-level window object has no children
	to walk in Typeless 2.11, so go to the Chromium render widget (or climb from the focus)."""
	try:
		from NVDAObjects.IAccessible import getNVDAObjectFromEvent
		hwnd = _user32.GetForegroundWindow()
		child = _user32.FindWindowExW(hwnd, None, "Chrome_RenderWidgetHostHWND", None) if hwnd else None
		if child:
			obj = getNVDAObjectFromEvent(child, OBJID_CLIENT, 0)
			if obj is not None:
				return obj
	except Exception:
		log.debugWarning("TypelessAccess: render widget lookup failed", exc_info=True)
	try:
		node = api.getFocusObject()
		last = None
		n = 0
		while node is not None and n < 60:
			n += 1
			if node.role == Role.DOCUMENT:
				return node
			last = node
			node = node.parent
		if last is not None:
			return last
	except Exception:
		pass
	return api.getForegroundObject()


_rootCache = {}


def _rootFor(obj):
	"""The web document that holds obj. Taken from the object's own window, so it is right
	even when Typeless is not the foreground window (stats read from another program)."""
	try:
		hwnd = obj.windowHandle
	except Exception:
		hwnd = None
	if hwnd:
		now = time.time()
		hit = _rootCache.get(hwnd)
		if hit is not None and 0 <= now - hit[1] < 2.0:
			return hit[0]
		try:
			from NVDAObjects.IAccessible import getNVDAObjectFromEvent
			root = getNVDAObjectFromEvent(hwnd, OBJID_CLIENT, 0)
		except Exception:
			root = None
		if root is not None:
			if len(_rootCache) > 20:
				_rootCache.clear()
			_rootCache[hwnd] = (root, now)
			return root
	return _typelessRoot()


NAV_TEXT = {
	"home": ("home", "الرئيسية (Home)"),
	"history": ("history", "السجل (History)"),
	"dictionary": ("dictionary", "القاموس (Dictionary)"),
}


def _nodeText(obj, depth=0):
	"""All the texts inside a control (Typeless 2.11 left the sidebar buttons without a name)."""
	parts = []
	try:
		child = obj.firstChild
	except Exception:
		child = None
	n = 0
	while child is not None and n < 20:
		n += 1
		try:
			nm = (child.name or "").strip()
		except Exception:
			nm = ""
		if nm:
			parts.append(nm)
		elif depth < 3:
			sub = _nodeText(child, depth + 1)
			if sub:
				parts.append(sub)
		try:
			child = child.next
		except Exception:
			child = None
	return " ".join(parts).strip()


def _navFromText(obj):
	try:
		txt = _nodeText(obj)
	except Exception:
		return None
	return NAV_TEXT.get(txt.lower())


def _dumpTree(root, limit=90):
	"""Write the controls of the window to the NVDA log (to diagnose a new Typeless version)."""
	lines = []
	stack = [(root, 0)]
	count = 0
	while stack and count < limit:
		node, depth = stack.pop()
		count += 1
		try:
			role = node.role
			name = (node.name or "").strip()
			loc = node.location
		except Exception:
			continue
		if role in CONTROL_ROLES or name:
			lines.append("%s%s | %s | %s | %s" % ("  " * min(depth, 6), role, name[:50], (_nodeText(node)[:40] if role in CONTROL_ROLES and not name else ""), tuple(loc) if loc else None))
		try:
			kids = []
			child = node.firstChild
			while child is not None and len(kids) < 100:
				kids.append(child)
				child = child.next
			for k in reversed(kids):
				stack.append((k, depth + 1))
		except Exception:
			continue
	log.info("TypelessAccess tree dump:\n" + "\n".join(lines))


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
		self.root = None

	def walk(self, obj, depth=0):
		if obj is None or depth > 60 or self.count > 5000:
			return
		if depth == 0 and self.root is None:
			self.root = obj
		self.count += 1
		try:
			role = obj.role
			name = (obj.name or "").strip()
		except Exception:
			return
		if role in CONTROL_ROLES:
			self._addControl(obj, role, name)
			return
		if name == "Check for updates":
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
				key = NAME_KEYS.get(name)
				if name == HELP_NAME:
					label = "المساعدة وميزات البرنامج وملاحظات الإصدار (Help)"
				elif name == "Add another" and context:
					label = "Add another - " + context[0]
			return label, key
		# Unnamed controls
		if role == Role.BUTTON:
			nav = _navFromText(obj)
			if nav:
				return nav[1], nav[0]
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
		return _sidebarIcon(obj, self.root)


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
_helpMissed = {}
_diag = [0]


def _diagLog(msg):
	"""A few info lines in the NVDA log to help diagnose problems."""
	if _diag[0] < 40:
		_diag[0] += 1
		log.info("TypelessAccess: " + msg)


_layoutLogged = [0.0]


def _layoutLog(hasHelp, siblings):
	"""When an icon cannot be named, record how its row looks (at most once a minute, outside the
	normal budget): it shows how a new Typeless version arranged the sidebar buttons."""
	now = time.time()
	if 0 <= now - _layoutLogged[0] < 60.0:
		return
	_layoutLogged[0] = now
	try:
		places = []
		for s in siblings:
			try:
				places.append(tuple(s.location) if s.location else None)
			except Exception:
				places.append(None)
		log.info("TypelessAccess: icon row layout: help named=%s, unnamed buttons=%d, places=%s" % (hasHelp, len(siblings), places))
	except Exception:
		pass


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
	# Typeless 2.11 has no such button: do not walk the whole window again for every icon.
	missed = _helpMissed.get(hwnd)
	now = time.time()
	if missed is not None and 0 <= now - missed < 10.0:
		return None
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
	_helpMissed[hwnd] = now
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


def _iconCacheKey(obj):
	"""(window, node id) when the node has a lasting id, else (window, place on screen).
	Returns the key and whether it is a lasting one."""
	try:
		hwnd = obj.windowHandle
	except Exception:
		return None, False
	try:
		uid = obj.IA2UniqueID
	except Exception:
		uid = None
	if uid is not None:
		return (hwnd, "id", uid), True
	try:
		loc = obj.location
	except Exception:
		loc = None
	if not loc:
		return None, False
	return (hwnd, "loc", tuple(loc)), False


def _sidebarIcon(obj, root=None):
	"""For the unnamed icons beside the Help button: returns (key, label) or None."""
	cacheKey, lasting = _iconCacheKey(obj)
	now = time.time()
	if cacheKey is not None:
		hit = _sidebarCache.get(cacheKey)
		# Answers expire: the page changes, and the window can be moved or resized.
		if hit is not None and 0 <= now - hit[1] < hit[2]:
			return hit[0]
	result = None
	siblings = []
	hasHelp = False
	try:
		parent = obj.parent
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
		elif len(siblings) == 4 and all(o.location and o.location[2] <= 100 for o in siblings):
			# Typeless 2.11: the Help button has no name any more, so there are 4 unnamed icons.
			siblings.sort(key=lambda o: o.location[0])
			icons = SIDEBAR_ICONS + [HELP_ICON]
			for i, s in enumerate(siblings):
				if s == obj:
					result = icons[i]
					break
	except Exception:
		result = None
	if result is None:
		if root is None:
			try:
				root = _rootFor(obj)
			except Exception:
				root = None
		try:
			result = _iconByGeometry(obj, root)
		except Exception:
			result = None
	_diagLog("unnamed button %s -> %s" % (cacheKey[1:] if cacheKey else None, result[1] if result else None))
	if result is None:
		_layoutLog(hasHelp, siblings)
	if cacheKey is not None:
		if len(_sidebarCache) > 500:
			_sidebarCache.clear()
		ttl = 30.0 if lasting else 3.0
		_sidebarCache[cacheKey] = (result, now, ttl)
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
_browseLabelCache = {}
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
					now = time.time()
					hit = _browseLabelCache.get((docHandle, ID))
					if hit is not None and 0 <= now - hit[1] < hit[2]:
						label = hit[0]
					else:
						# Building the object is the slow part: remember the answer for a while.
						buf = self.obj
						obj = buf.getNVDAObjectFromIdentifier(docHandle, ID)
						root = getattr(buf, "rootNVDAObject", None)
						found = _sidebarIcon(obj, root) if obj else None
						label = found[1] if found else None
						if len(_browseLabelCache) > 500:
							_browseLabelCache.clear()
						_browseLabelCache[(docHandle, ID)] = (label, now, 30.0 if label else 3.0)
						if label:
							_diagLog("browse label %s" % label)
					if label:
						attrs["_typelessLabel"] = label
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


def _stateWord(role, states):
	"""The spoken on/off state of a checkbox, switch or toggle button ("" for other controls)."""
	if role == Role.CHECKBOX:
		return "محددة" if State.CHECKED in states else "غير محددة"
	if ROLE_SWITCH is not None and role == ROLE_SWITCH:
		on = (STATE_ON is not None and STATE_ON in states) or State.CHECKED in states
		return "مشغّل" if on else "متوقف"
	if role == Role.TOGGLEBUTTON:
		on = (STATE_PRESSED is not None and STATE_PRESSED in states) or State.CHECKED in states
		return "مضغوط" if on else "غير مضغوط"
	return ""


def _describe(item):
	obj = item["obj"]
	role = item["role"]
	text = item["label"]
	word = ROLE_WORDS.get(role, "")
	try:
		states = obj.states
	except Exception:
		states = set()
	stateWord = _stateWord(role, states)
	if stateWord:
		word += " " + stateWord
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
			# The clickable part may be the text itself or one of its parents.
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
				raise RuntimeError("no clickable parent")
		else:
			obj.doAction()
	except Exception:
		log.debugWarning("Typeless Access: doAction failed", exc_info=True)
		ui.message("تعذر تفعيل العنصر")
		return
	role = item["role"]
	if _stateWord(role, set()):
		def report():
			try:
				ui.message(_stateWord(role, obj.states))
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
		# Every Typeless launch gets a fresh budget of log lines, and the log says which version it is.
		_diag[0] = 0
		try:
			version = self.productVersion
		except Exception:
			version = "?"
		_diagLog("app module loaded, Typeless %s, browse mode labels %s" % (version, "ready" if _TypelessVBuf is not None else "unavailable"))

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
				from NVDAObjects.IAccessible import IAccessible as _IA2Base
				if isinstance(obj, _IA2Base):
					clsList.insert(0, TypelessDocument)
			elif role == Role.BUTTON and not _rawName(obj):
				clsList.insert(0, SidebarIconButton)
		except Exception:
			log.debugWarning("TypelessAccess overlay", exc_info=True)

	def _collect(self):
		root = _typelessRoot()
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
		try:
			# Leave a trace in the NVDA log: it shows what a new Typeless version changed.
			_dumpTree(_typelessRoot())
		except Exception:
			log.debugWarning("Typeless Access: dump failed", exc_info=True)

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
		done = None
		try:
			for item in self._collect():
				if item["key"] == "updates":
					target = item["obj"]
					done = False
					for attempt in range(4):
						if target is None:
							break
						try:
							target.doAction()
							done = True
							break
						except Exception:
							pass
						try:
							target = target.parent
						except Exception:
							target = None
					break
			if done is None:
				done = _clickTextControl(_typelessRoot(), "Check for updates")
		except Exception:
			done = False
		if done:
			ui.message("جارٍ البحث عن تحديثات")
		elif done is None:
			ui.message("لم أجد Check for updates. افتح الإعدادات بـ Alt مع 4 ثم تبويب About")
		else:
			ui.message("تعذر الضغط على Check for updates")

	@script(description="Typeless: قراءة إحصاءاتك والسلاسل (Streaks)", gesture="kb:alt+9", category=CATEGORY)
	def script_readStats(self, gesture):
		try:
			parts = _statsSummary(_typelessRoot())
		except Exception:
			log.debugWarning("TypelessAccess stats", exc_info=True)
			parts = []
		if parts:
			ui.message("، ".join(parts))
		else:
			ui.message("لم أجد الإحصاءات. افتح الصفحة الرئيسية أولاً بـ Alt مع 1")
