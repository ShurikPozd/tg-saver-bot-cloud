"""Нет «мёртвых» кнопок: данные каждой inline-кнопки в коде кто-то обрабатывает в on_callback.
Ловит опечатки вроде data="svc_order" без обработчика (кнопка молча ничего не делает).
Запуск: python -m unittest discover tests"""

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name):
    return open(os.path.join(ROOT, name), encoding="utf-8").read()


class ButtonsHandledTests(unittest.TestCase):
    def test_every_callback_prefix_has_handler(self):
        code = _read("bot.py") + _read("keyboards.py")
        # что нажимается: data="x", data=f"x:{...}", data=f"x|{...}"
        prefixes = set()
        for m in re.finditer(r'data=f?"([^"]*)"', code):
            raw = m.group(1)
            head = re.split(r"[{]", raw, maxsplit=1)[0]
            if not head or head.startswith(("h:",)):
                continue
            prefixes.add(head)
        bot = _read("bot.py")
        exact = set(re.findall(r'data_s == "([^"]+)"', bot))
        starts = set()
        for m in re.finditer(r"data_s\.startswith\(\(?([^)]*)\)", bot):
            starts.update(re.findall(r'"([^"]+)"', m.group(1)))
        # обработчики внутри подменю (op == "tri_ok", data_s in ("a", "b"))
        exact.update(re.findall(r'op == "([^"]+)"', bot))
        for m in re.finditer(r"data_s in \(([^)]*)\)", bot):
            exact.update(re.findall(r'"([^"]+)"', m.group(1)))

        def handled(p):
            if p in exact or any(p.startswith(s) for s in starts):
                return True
            # «x:» / «x|» с параметром — достаточно startswith по самому префиксу
            return any(s.startswith(p) or p.startswith(s) for s in starts if len(s) >= 3)

        missing = sorted(p for p in prefixes if not handled(p))
        self.assertEqual(missing, [], f"кнопки без обработчика: {missing}")


if __name__ == "__main__":
    unittest.main()
