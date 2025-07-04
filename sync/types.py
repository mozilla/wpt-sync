from typing import Mapping, Sequence

Json = Mapping[str, "Json"] | Sequence["Json"] | str | int | float | bool | None
