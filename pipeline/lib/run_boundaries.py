"""Where one experiment run ends and the next begins.

Three places need this answer and they must all give the same one: S6, which
scores runs; `eval/validate_detection.py`, which lines those scores up against
the labels; and `ui/series_reader.py`, which draws them. Each used to restate
the rule in its own words, on the reasoning that eval and the UI should not
reach into a stage. This is a library, not a stage, so they can share it -- and
they now have to, because the three copies had already drifted.

The rule they shared was: a run starts where col_time returns to 1, numbered in
Parquet row order. That reads the boundaries off the physical row order, and
the physical row order is not what it looks like. DuckDB's partitioned writer
rotates its buffer, so a partition comes back as 48..500 followed by 1..47.
Splitting on col_time == 1 then glues the head of a run onto the tail of the
one before it. In this dataset 54 of the 500 partitions are rotated somewhere,
and only 14 of them at row zero -- a "does the partition start at 1?" guard
sees a quarter of the damage and calls the rest clean.

So the boundaries come out of col_time and nothing else. Every run is a
contiguous 1..L. Cut the partition where col_time does not advance by exactly
one, then hand each headless fragment back the fragment holding its missing
first samples.
"""
import numpy as np


def fragments(t):
    """Maximal slices over which col_time increases by exactly one."""
    br = np.where(np.diff(t) != 1)[0] + 1
    starts = np.concatenate([[0], br]).astype(int)
    ends = np.concatenate([br, [len(t)]]).astype(int)
    return list(zip(starts, ends))


def rebuild_runs(t, where, warn=print):
    """Row indices of each run in this partition, in order.

    `where` names the partition in any message. A fragment whose head is
    missing from the partition entirely is kept as a short run and reported
    through `warn` -- it is not stitched onto its neighbour, because a run that
    does not start at 1 is data to be honest about, not data to repair.
    """
    t = np.asarray(t)
    if len(t) == 0:
        raise ValueError(f"{where}: partition is empty.")

    frags = fragments(t)
    used, blocks = set(), []
    for k, (a, b) in enumerate(frags):
        if k in used:
            continue
        if t[a] == 1:
            blocks.append([(a, b)])
            continue
        need = int(t[a]) - 1
        head = [j for j in range(k + 1, len(frags))
                if j not in used and t[frags[j][0]] == 1
                and t[frags[j][1] - 1] == need]
        if not head:
            warn(f"  ! {where}: a run starts at col_time={int(t[a])} and no "
                 f"fragment holds its first {need} samples. Kept as a partial "
                 f"run; it is short, not stitched onto its neighbour.")
            blocks.append([(a, b)])
            continue
        used.add(head[0])
        blocks.append([frags[head[0]], (a, b)])

    out = []
    for parts in blocks:
        idx = np.concatenate([np.arange(a, b) for a, b in parts])
        seq = t[idx]
        if not np.array_equal(seq, np.arange(seq[0], seq[0] + len(seq))):
            raise ValueError(
                f"{where}: a rebuilt run is not contiguous in col_time. The "
                f"partition is damaged beyond what row order alone explains.")
        out.append(idx)
    return out
