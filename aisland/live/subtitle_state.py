"""Tracks the utterance currently shown in the compact subtitle view.

Live drafts use ``L{gen}`` sids while final segments use uuids, and a final
translation may arrive after the next utterance has already started. Keeping
the current utterance's sids plus the previous utterance's sids lets the UI
drop late results without changing the pipeline's sid scheme.
"""
from __future__ import annotations


class SubtitleGate:
    def __init__(self):
        self._sids: set[str] = set()
        self._previous: set[str] = set()
        self._finalized = True
        self._started = False

    def begin_if_new(self, jp: str) -> bool:
        """An empty partial marks the end of an utterance.

        Returns True when a new utterance starts, so the caller can clear the
        previous Chinese line.
        """
        if not jp:
            self._finalized = True
            return False
        if self._finalized:
            self._previous = set(self._sids)
            self._sids.clear()
            self._started = False
            self._finalized = False
            return True
        return False

    def add_segment(self, sid: str) -> None:
        self._sids.add(sid)
        self._started = True

    def accept_live(self, sid: str) -> bool:
        """A draft translation: only the first sid of a fresh utterance."""
        if sid in self._previous:
            return False
        if sid in self._sids:
            return True
        if self._started or self._sids:
            return False
        self._sids.add(sid)
        return True

    def accept_final(self, sid: str) -> bool:
        """A full-sentence translation: its segment row must still be current."""
        return self._started and sid in self._sids and sid not in self._previous
