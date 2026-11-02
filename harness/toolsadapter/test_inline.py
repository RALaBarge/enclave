#!/usr/bin/env python3
"""Tests for the inline/spill policy.

The invariants that matter, in order: the budget is a hard ceiling; fields that
say whether a result is complete survive; a cut is never silent; and the same
input gives the same output (the JS mirror and the future Go client are diffed
against this module, so a nondeterministic policy would be untestable there).
"""
from __future__ import annotations

import json
import unittest

import inline


def read_result(content: str, total_lines: int = 5000, **extra) -> dict:
    return {"content": content, "total_lines": total_lines, **extra}


class UnderBudget(unittest.TestCase):
    def test_an_ordinary_result_is_returned_untouched(self):
        value = {"content": "one\ntwo\n", "total_lines": 2}
        out, info = inline.shrink(value)
        self.assertIs(out, value)
        self.assertFalse(info.spilled)
        self.assertEqual(info.elided_chars, 0)

    def test_render_of_a_small_result_is_just_the_json(self):
        value = {"matches": ["a.py:1:x"], "truncated": False}
        text, info = inline.render(value)
        self.assertEqual(text, inline._dumps(value))
        self.assertFalse(info.spilled)


class FieldAware(unittest.TestCase):
    def test_a_large_content_field_is_cut(self):
        value = read_result("x" * 100_000)
        out, info = inline.shrink(value)
        self.assertLess(len(out["content"]), 20_000)
        self.assertTrue(info.spilled)
        self.assertIn("content", info.fields_elided)

    def test_the_fields_that_say_a_result_is_complete_survive(self):
        # These are the fields a blind cut of the serialised form would take out
        # first, and the ones the model needs to know the answer is partial.
        value = {"stdout": "y" * 200_000, "stderr": "", "exit_code": 3, "timed_out": True, "error": "boom"}
        text, info = inline.render(value)
        self.assertIn('"exit_code":3', text)
        self.assertIn('"timed_out":true', text)
        self.assertIn('"error":"boom"', text)
        self.assertTrue(info.spilled)

    def test_a_small_field_is_never_cut(self):
        value = {"content": "z" * 100_000, "error": "short and important"}
        out, _ = inline.shrink(value)
        self.assertEqual(out["error"], "short and important")

    def test_nested_fields_are_cut_by_path(self):
        value = {"job": {"result": {"content": "q" * 90_000}}, "ok": True}
        out, info = inline.shrink(value)
        self.assertLess(len(out["job"]["result"]["content"]), 20_000)
        self.assertEqual(out["ok"], True)
        self.assertTrue(any(p.endswith("content") for p in info.fields_elided))


class Budget(unittest.TestCase):
    def test_render_respects_the_budget_on_every_shape(self):
        shapes = {
            "read": read_result("a" * 500_000),
            "bash": {"stdout": "b" * 300_000, "stderr": "c" * 300_000, "exit_code": 0, "timed_out": False},
            "grep": {"matches": ["f.py:%d:%s" % (i, "d" * 4000) for i in range(500)], "truncated": True},
            "string": "e" * 400_000,
            "tiny-keys": {f"k{i}": "v" * 10 for i in range(20_000)},
        }
        for name, value in shapes.items():
            with self.subTest(name):
                text, _ = inline.render(value, max_chars=4000)
                self.assertLessEqual(len(text), 4000)

    def test_a_smaller_budget_cuts_more(self):
        value = read_result("w" * 200_000)
        _, big = inline.shrink(value, max_chars=16_000)
        _, small = inline.shrink(value, max_chars=2_000)
        self.assertGreater(small.elided_chars, big.elided_chars)

    def test_a_result_with_nothing_to_cut_is_hard_cut_and_says_so(self):
        value = {f"k{i}": "v" * 10 for i in range(20_000)}
        text, info = inline.render(value, max_chars=4000)
        self.assertTrue(info.hard_cut)
        self.assertLessEqual(len(text), 4000)


class HonestCut(unittest.TestCase):
    def test_a_cut_says_how_much_was_elided(self):
        value = read_result("x" * 100_000)
        out, _ = inline.shrink(value)
        self.assertIn("chars elided", out["content"])

    def test_the_marker_names_the_recovery(self):
        value = read_result("x" * 100_000)
        out, _ = inline.shrink(value)
        self.assertIn("offset", out["content"])
        self.assertIn("limit", out["content"])

    def test_a_grep_marker_names_the_pattern(self):
        value = {"matches": ["f.py:%d:%s" % (i, "d" * 4000) for i in range(500)], "truncated": True}
        text, _ = inline.render(value)
        self.assertIn("tighten the pattern", text)

    def test_head_and_tail_are_both_kept(self):
        value = read_result("HEAD" + "m" * 100_000 + "TAIL")
        out, _ = inline.shrink(value)
        self.assertTrue(out["content"].startswith("HEAD"))
        self.assertTrue(out["content"].endswith("TAIL"))

    def test_a_cut_prefers_a_line_break(self):
        text = "".join(f"line {i}\n" for i in range(10_000))
        out = inline._cut(text, 2_000, len(text), hint="narrow")
        head = out.split("…[")[0]
        self.assertTrue(head.endswith("\n"), "head should end on a line boundary")


class Lists(unittest.TestCase):
    def test_a_long_match_list_is_capped_with_a_marker(self):
        value = {"matches": [f"f.py:{i}:" + "d" * 1000 for i in range(500)], "truncated": True}
        out, info = inline.shrink(value)
        self.assertLess(len(out["matches"]), 500)
        self.assertGreater(info.items_elided, 0)
        self.assertTrue(out["matches"][-1].startswith("…["))

    def test_a_list_under_the_limit_keeps_every_item(self):
        value = {"matches": [f"f.py:{i}" for i in range(3)]}
        out, info = inline.shrink(value)
        self.assertEqual(out["matches"], value["matches"])
        self.assertEqual(info.items_elided, 0)


class Determinism(unittest.TestCase):
    def test_the_same_input_gives_the_same_output(self):
        value = {"stdout": "s" * 50_000, "exit_code": 0, "matches": ["m" * 900] * 40}
        first = inline.render(value)
        second = inline.render(value)
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1].as_dict(), second[1].as_dict())

    def test_shrink_does_not_mutate_its_argument(self):
        value = read_result("x" * 100_000)
        before = json.dumps(value)
        inline.shrink(value)
        self.assertEqual(json.dumps(value), before)


if __name__ == "__main__":
    unittest.main()
