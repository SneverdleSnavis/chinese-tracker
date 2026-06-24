from functools import lru_cache
import opencc


@lru_cache(maxsize=1)
def _converter():
    return opencc.OpenCC("t2s")


def to_simplified(text: str) -> str:
    return _converter().convert(text)
