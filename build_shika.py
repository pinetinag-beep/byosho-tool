"""歯科の施設基準の届出を shika_cache.parquet にまとめる（build_yakkyoku.py --kind 歯科 と同じ）。

    python build_shika.py     # shisetsu_raw_shika/ の xlsx をすべて読む
"""
import sys

import build_yakkyoku as by

if __name__ == "__main__":
    by.build("歯科")
    sys.exit(0)
