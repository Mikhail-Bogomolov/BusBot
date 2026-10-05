from typing import Protocol

from ..models import Vehicle


class Source(Protocol):
    async def fetch(self) -> list[Vehicle]: ...
