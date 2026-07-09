from __future__ import annotations

from pathlib import Path

from swegen.farm.state import StreamState


class LocalStateStore:
    """Farm state on local disk - the historical behavior.

    Lost when an ephemeral sandbox dies; use GitStateStore when publishing.
    """

    def __init__(self, state_file: Path) -> None:
        self.state_file = Path(state_file)

    def load(self, repo: str) -> StreamState:
        return StreamState.load(self.state_file, repo)

    def save(self, state: StreamState) -> None:
        state.save(self.state_file)
