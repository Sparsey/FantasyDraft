"""Generate a synthetic player pool (data/sample_players.csv) for demos and tests.

Names and numbers are made up — use `fetch-players` (or your own spreadsheet
export) for the real thing.
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fantasydraft.io import save_players  # noqa: E402
from fantasydraft.models import Player  # noqa: E402

POS_MIX = ["PG", "SG", "SF", "PF", "C", "PG/SG", "SG/SF", "SF/PF", "PF/C", "G", "F"]
EXPAND = {"G": "PG/SG", "F": "SF/PF"}


def main(n: int = 220, seed: int = 7) -> None:
    rng = random.Random(seed)
    raw = []
    for i in range(n):
        pts = 3200 * (0.985 ** i) * rng.uniform(0.92, 1.08)
        pos = rng.choice(POS_MIX)
        pos = EXPAND.get(pos, pos)
        raw.append((frozenset(pos.split("/")), pts))
    raw.sort(key=lambda r: -r[1])
    players = []
    for i, (pos, pts) in enumerate(raw):
        name = f"Player {i + 1:03d}"
        espn_rank = i + 1 + rng.gauss(0, 3 + 0.05 * i)  # ESPN rank ~ follows points, with error
        adp = espn_rank + rng.gauss(0, 2 + 0.08 * i)  # ADP ~ follows ESPN rank, with more error
        players.append(Player(str(1000 + i), name, pos, round(pts, 1), round(max(espn_rank, 1), 1), round(max(adp, 1), 1)))
    # ESPN rank and ADP are orders, not floats
    for key in ("espn_rank", "adp"):
        ordered = sorted(players, key=lambda p: getattr(p, key))
        for rank, p in enumerate(ordered, 1):
            object.__setattr__(p, key, float(rank))
    out = Path(__file__).resolve().parent.parent / "data" / "sample_players.csv"
    save_players(players, out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
