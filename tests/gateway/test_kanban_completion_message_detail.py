"""Completion notices carry the usable result, not just its first sentence."""

import unittest
from types import SimpleNamespace

from gateway.kanban_watchers_notifier import _fmt_completed


class CompletionNoticeDetailTests(unittest.TestCase):
    def _format(self, summary, *, exact_artifact=None):
        payload = {"summary": summary}
        if exact_artifact is not None:
            payload["exact_artifact"] = exact_artifact
        event = SimpleNamespace(payload=payload)
        notice = SimpleNamespace(head="t_example", title="Board report", task=None)
        return _fmt_completed(event, notice)

    def test_delivers_the_next_action_after_the_first_sentence(self):
        summary = "Two cards are blocked.\nFirst: keep the failed source-map card blocked.\nSecond: archive the stale local probe after review."
        message, wake, _ = self._format(summary)
        self.assertIn("Second: archive the stale local probe after review.", message)
        self.assertIn("Two cards are blocked.", wake)

    def test_long_summary_has_bounded_explicit_truncation(self):
        message, _, _ = self._format("finding " * 500)
        self.assertLessEqual(len(message), 1800)
        self.assertTrue(message.endswith("..."))

    def test_verified_exact_receipt_is_preserved_after_summary(self):
        receipt = {"status": "verified", "relative_path": "proof.txt", "size": 21, "sha256": "a" * 64}
        message, wake, _ = self._format("Created the proof file", exact_artifact=receipt)
        self.assertIn("proof.txt · 21 bytes", message)
        self.assertIn("a" * 64, message)
        self.assertIn("proof.txt · 21 bytes", wake)

    def test_unverified_receipt_is_not_promoted(self):
        receipt = {"status": "candidate", "relative_path": "proof.txt", "size": 21, "sha256": "a" * 64}
        message, _, _ = self._format("Created the proof file", exact_artifact=receipt)
        self.assertNotIn("SHA-256", message)


if __name__ == "__main__":
    unittest.main()
