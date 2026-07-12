from __future__ import annotations

from .base import PublishContext, PublishResult


class NullSink:
    """Default sink: keeps tasks on local disk only.

    Used whenever --publish-repo is absent, so `swegen create` and `swegen farm`
    behave exactly as they did before publishing existed.
    """

    def preflight(self) -> None:
        return None

    def publish(self, ctx: PublishContext) -> PublishResult:
        return PublishResult(published=False)
