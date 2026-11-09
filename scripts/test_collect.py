import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import collect


class HistoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.out = Path(temp.name) / "data.json"
        self.out_js = Path(temp.name) / "data.js"
        self.history = json.loads(Path(collect.HISTORY).read_text())
        self.history_count = sum(len(p["commits"]) for p in self.history)
        contexts = contextlib.ExitStack()
        self.addCleanup(contexts.close)
        contexts.enter_context(patch.multiple(collect, OUT=str(self.out), OUT_JS=str(self.out_js)))
        self.fetch = contexts.enter_context(patch.object(collect, "commits_for", return_value=[]))
        contexts.enter_context(patch.object(collect.time, "sleep"))
        contexts.enter_context(contextlib.redirect_stdout(io.StringIO()))
        contexts.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def generate(self):
        collect.main()
        data = json.loads(self.out.read_text())
        js = self.out_js.read_text().removeprefix("window.CONTRIB = ").strip().removesuffix(";")
        self.assertEqual(data, json.loads(js))
        return data

    def test_history_survives_repeated_empty_searches_without_extra_queries(self):
        historical_urls = {c["url"] for p in self.history for c in p["commits"]}
        for _ in range(2):
            data = self.generate()
            self.assertEqual(data["totals"]["commits"], self.history_count)
            self.assertEqual(data["totals"]["projects"], len(self.history))
            self.assertEqual(data["totals"]["first"], "2016-08-29")
            self.assertEqual({c["url"] for p in data["projects"] for c in p["commits"]}, historical_urls)
        self.assertEqual([c.args[0] for c in self.fetch.call_args_list],
                         [r[0] for r in collect.REPOS] * 2)

    def test_failed_search_keeps_cache_and_history(self):
        commit = {"sha": "a" * 10, "date": "2026-01-01", "title": "Cached commit",
                  "url": "https://github.com/jemalloc/jemalloc/commit/" + "a" * 40}
        self.out.write_text(json.dumps({"projects": [{"repo": "jemalloc/jemalloc", "commits": [commit]}]}))
        self.fetch.return_value = None
        data = self.generate()
        self.assertEqual(data["totals"]["commits"], self.history_count + 1)
        project = next(p for p in data["projects"] if p["repo"] == "jemalloc/jemalloc")
        self.assertEqual(project["commits"], [commit])

    @patch.object(collect, "REPOS", collect.REPOS[:1])
    def test_search_misses_never_remove_published_commits(self):
        repo = collect.REPOS[0][0]
        old, new = ({"sha": d * 10, "date": date, "title": f"Change {d}",
                     "url": f"https://github.com/{repo}/commit/" + d * 40}
                    for d, date in (("a", "2026-01-01"), ("b", "2026-01-02")))
        self.fetch.return_value = [old]
        self.generate()
        # an empty result, then a partial one that only has the newest commit
        for found, expected in (([], [old]), ([new], [new, old])):
            self.fetch.return_value = found
            project = next(p for p in self.generate()["projects"] if p["repo"] == repo)
            self.assertEqual(project["commits"], expected)

    def test_history_file_stays_authoritative_for_unsearched_repos(self):
        historical = next(p for p in self.history if p["repo"] == "knative/serving")
        removed = {"sha": "c" * 10, "date": "2020-01-01", "title": "Removed from history.json",
                   "url": "https://github.com/knative/serving/commit/" + "c" * 40}
        self.out.write_text(json.dumps({"projects": [
            {"repo": historical["repo"], "commits": historical["commits"] + [removed]}]}))
        project = next(p for p in self.generate()["projects"] if p["repo"] == historical["repo"])
        self.assertEqual(project["commits"], historical["commits"])

    def test_live_history_overlap_keeps_pinned_attribution_without_duplicates(self):
        historical = next(p for p in self.history if p["repo"] == "knative/serving")
        pinned = historical["commits"][0]
        duplicate = {**pinned, "sha": pinned["url"].rsplit("/", 1)[-1]}
        duplicate.pop("pr")
        new_commit = {"sha": "b" * 10, "date": "2026-01-01", "title": "New commit",
                      "url": "https://github.com/knative/serving/commit/" + "b" * 40}
        self.fetch.return_value = [duplicate, new_commit]
        with patch.object(collect, "REPOS", [(historical["repo"], historical["era"],
                                             historical["label"], historical["lang"])]):
            data = self.generate()
        project = next(p for p in data["projects"] if p["repo"] == historical["repo"])
        self.assertEqual(project["commits"], [new_commit, pinned])
        self.assertEqual((project["count"], project["first"], project["last"]),
                         (2, pinned["date"], new_commit["date"]))
        self.assertEqual(data["totals"]["commits"], self.history_count + 1)
        self.fetch.assert_called_once_with(historical["repo"])

    @patch.object(collect, "REPOS", collect.REPOS[:1])
    @patch.object(collect.datetime, "datetime")
    def test_daily_run_changes_outputs_only_when_commits_change(self, clock):
        repo = collect.REPOS[0][0]
        commits = [
            {"sha": digit * 10, "date": "2026-01-01", "title": f"Change {digit}",
             "url": f"https://github.com/{repo}/commit/" + digit * 40}
            for digit in ("a", "b", "c")
        ]
        self.fetch.return_value = commits[:2]
        clock.now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
        first = self.generate()
        json_before, js_before = self.out.read_bytes(), self.out_js.read_bytes()

        clock.now.return_value = datetime(2026, 1, 2, tzinfo=timezone.utc)
        self.fetch.return_value.reverse()
        self.assertEqual(self.generate(), first)
        self.assertEqual(self.out.read_bytes(), json_before)
        self.assertEqual(self.out_js.read_bytes(), js_before)

        self.fetch.return_value.append(commits[2])
        updated = self.generate()
        self.assertEqual(updated["generated"], "2026-01-02 00:00 UTC")
        self.assertEqual(updated["totals"]["commits"], first["totals"]["commits"] + 1)

    def test_unchanged_data_repairs_stale_browser_copy(self):
        original = self.generate()
        self.out_js.write_text("window.CONTRIB = {};\n")
        self.assertEqual(self.generate(), original)


if __name__ == "__main__":
    unittest.main()
