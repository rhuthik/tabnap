import os
import unittest

from tabnap import groups

H = os.path.expanduser("~")


class Match(unittest.TestCase):
    def setUp(self):
        self.rules = groups.compile_rules({"oncall": r"CSTFP-\d+|^~/Documents/Moneyone/Oncall(/|$)",
                                           "router": "smart-router", "bad": "(unclosed"})

    def test_title_or_folder_first_match_wins(self):
        self.assertEqual(groups.match(self.rules, "zz CSTFP-1821 Jira issue", H + "/x"), "oncall")
        self.assertEqual(groups.match(self.rules, "bajaj monitoring", H + "/Documents/Moneyone/Oncall/projects/b"), "oncall")
        self.assertEqual(groups.match(self.rules, "ujjivan", H + "/Documents/Moneyone/smart-router"), "router")
        self.assertEqual(groups.match(self.rules, "anything", H + "/Documents/Moneyone/OncallX"), "")

    def test_bad_regex_is_skipped(self):
        self.assertEqual([n for n, _ in self.rules], ["oncall", "router"])


class Suggest(unittest.TestCase):
    def test_deepest_shared_folder_and_skips_broad_ones(self):
        m = H + "/Documents/Moneyone"
        sessions = [{"cwd": m + "/Oncall"}, {"cwd": m + "/Oncall"}, {"cwd": m + "/Oncall/projects/bajaj"},
                    {"cwd": m + "/smart-router"}, {"cwd": m + "/smart-router"},
                    {"cwd": m + "/parse-x"}, {"cwd": H}, {"cwd": H}, {"cwd": H + "/Documents"},
                    {"cwd": H + "/Documents"}, {"cwd": m + "/Oncall", "group": "already"}]
        rules = groups.suggest(sessions)
        self.assertEqual(rules, {"oncall": "^~/Documents/Moneyone/Oncall(/|$)",
                                 "smart-router": "^~/Documents/Moneyone/smart-router(/|$)"})
        compiled = groups.compile_rules(rules)
        self.assertEqual(groups.match(compiled, "", m + "/Oncall/projects/bajaj"), "oncall")
        self.assertEqual(groups.suggest([{"cwd": H + "/a/b.c (x)"}] * 2), {"b-c-x": r"^~/a/b\.c \(x\)(/|$)"})


class PickGroup(unittest.TestCase):
    def test_exact_name_only(self):
        names = ["oncall", "cost & infra"]
        self.assertEqual(groups.pick_group(" `Cost & Infra`.\n", names), "cost & infra")
        self.assertEqual(groups.pick_group("none", names), "")
        self.assertEqual(groups.pick_group("I think oncall", names), "")


class Collapse(unittest.TestCase):
    def test_only_when_all_asleep_and_unfocused(self):
        z = {"state": "hibernated", "current": False}
        self.assertTrue(groups.want_collapsed([z, z]))
        self.assertFalse(groups.want_collapsed([z, {"state": "idle", "current": False}]))
        self.assertFalse(groups.want_collapsed([z, {"state": "hibernated", "current": True}]))
        self.assertFalse(groups.want_collapsed([]))


if __name__ == "__main__":
    unittest.main()
