"""Smoke: force-join section refactor — keyboards, callbacks, texts."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from html.parser import HTMLParser

# ── wiring ──
import bot  # noqa: F401  (full router/middleware import graph)
import handlers.admin as ha
import keyboards.admin as ka
import keyboards

# ── fakes ──
cfg_on = SimpleNamespace(enabled=True, require_join=True, max_length=500)
cfg_off = SimpleNamespace(enabled=False, require_join=False, max_length=500)
rows = [
    SimpleNamespace(id=7, chat_id=-100111, title="News & Updates", username="", position=1, is_active=True),
    SimpleNamespace(id=8, chat_id=-100222, title="کانال دوم", username="second", position=2, is_active=True),
]


def cbs(markup):
    return [
        btn.callback_data
        for row in markup.inline_keyboard
        for btn in row
        if btn.callback_data
    ]


def widths(markup):
    return [len(r) for r in markup.inline_keyboard]


failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok  {name}")
    else:
        failures.append(name)
        print(f"FAIL  {name} {detail}")


# ── 1. main panel: new button, layout still 2-per-row ──
panel = ka.admin_panel_kb(is_root=False)
panel_data = cbs(panel)
check("panel has admin:forcejoin", "admin:forcejoin" in panel_data)
check("panel button count = 10", len(panel_data) == 10, panel_data)
check("panel widths <= 2", max(widths(panel)) <= 2, widths(panel))
panel_root = ka.admin_panel_kb(is_root=True)
check("root panel button count = 12", len(cbs(panel_root)) == 12, cbs(panel_root))
check("root widths <= 2", max(widths(panel_root)) <= 2, widths(panel_root))

# ── 2. whisper keyboard: force-join buttons gone ──
wkb = cbs(ka.admin_whisper_kb(cfg_on))
check("whisper kb has no join buttons",
      not any("join" in c or "channels" in c for c in wkb), wkb)
check("whisper kb keeps toggle/stats/back",
      {"admin:whisper:toggle", "admin:whisper:stats", "admin:panel"} <= set(wkb), wkb)

# ── 3. force-join keyboards ──
fkb = cbs(ka.admin_forcejoin_kb(cfg_on, rows))
check("forcejoin kb callbacks",
      {"admin:forcejoin:toggle", "admin:forcejoin:channels",
       "admin:forcejoin:channels:new", "admin:panel"} <= set(fkb), fkb)
check("forcejoin kb one per row", widths(ka.admin_forcejoin_kb(cfg_on, rows)) == [1, 1, 1, 1])
check("forcejoin toggle label = disable when on",
      ka.admin_forcejoin_kb(cfg_on, rows).inline_keyboard[0][0].text.endswith("غیرفعال کردن عضویت اجباری"),
      ka.admin_forcejoin_kb(cfg_on, rows).inline_keyboard[0][0].text)

lkb_m = ka.admin_forcejoin_channels_kb(rows, not_admin_ids={-100111})
lkb = cbs(lkb_m)
check("list kb del callbacks",
      "admin:forcejoin:channels:del:7" in lkb and "admin:forcejoin:channels:del:8" in lkb, lkb)
check("list kb back -> forcejoin", "admin:forcejoin" in lkb)
check("list kb warns unchecked row", "⚠️" in lkb_m.inline_keyboard[0][0].text)
check("list kb keeps clean row clean", "⚠️" not in lkb_m.inline_keyboard[1][0].text)
check("list kb label unescaped (no &amp;)", "&amp;" not in lkb_m.inline_keyboard[0][0].text)

# ── 4. texts ──
class Balance(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.bad = []
    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)
    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.bad.append(tag)
        else:
            self.stack.pop()

def assert_html(name, text):
    p = Balance()
    p.feed(text)
    check(name, not p.bad and not p.stack, f"bad={p.bad} open={p.stack}")

t1 = ha._forcejoin_text(rows, cfg_on, {-100111})
assert_html("_forcejoin_text(with rows)", t1)
check("forcejoin text shows count", "تعداد کانال‌ها: <b>2</b>" in t1)
check("forcejoin text warns unchecked", "ادمین نیست" in t1)
check("forcejoin text escapes title", "News &amp; Updates" in t1, t1)

t2 = ha._forcejoin_text([], cfg_off, set())
assert_html("_forcejoin_text(empty)", t2)
check("empty text hints add", "افزودن کانال" in t2)
check("empty text notes off-state", "خاموش" in t2)

import asyncio
t3 = asyncio.run(ha._whisper_config_text(cfg_on))
assert_html("_whisper_config_text", t3)
check("whisper text has breadcrumb", "عضویت اجباری" in t3, t3)
check("whisper text no join-state line", "عضویت اجباری:" not in t3, t3)

# ── 5. exports ──
check("keyboards package exports new names",
      hasattr(keyboards, "admin_forcejoin_kb") and hasattr(keyboards, "admin_forcejoin_channels_kb"))
check("old export gone", not hasattr(keyboards, "admin_whisper_channels_kb"))

# ── 6. router registrations for the new section ──
cb_names = {h.callback.__name__ for h in ha.router.callback_query.handlers}
msg_names = {h.callback.__name__ for h in ha.router.message.handlers}
for expect in ("cb_forcejoin_menu", "cb_forcejoin_toggle", "cb_forcejoin_channels",
               "cb_forcejoin_channels_new", "cb_forcejoin_channel_del",
               "cb_forcejoin_legacy_redirect"):
    check(f"router registers {expect}", expect in cb_names)
check("router registers msg_forcejoin_channel", "msg_forcejoin_channel" in msg_names)
check("old handlers unregistered",
      not ({"cb_whisper_join_toggle", "cb_whisper_join_unset", "cb_whisper_join_new",
            "msg_whisper_chat", "cb_whisper_channels", "cb_whisper_channel_del",
            "msg_whisper_channel"} & cb_names))

print()
if failures:
    print("FAILURES:", failures)
    sys.exit(1)
print("ALL SMOKE CHECKS PASSED")
