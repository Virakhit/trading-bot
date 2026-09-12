from dataclasses import dataclass


@dataclass(frozen=True)
class WalkForwardSplit:
    train: tuple
    validation: tuple
    out_of_sample: tuple


def split_walk_forward(items, train_ratio=.6, validation_ratio=.2) -> WalkForwardSplit:
    values = tuple(items)
    if not 0 < train_ratio < 1 or not 0 < validation_ratio < 1 or train_ratio + validation_ratio >= 1:
        raise ValueError("Ratios must leave an out-of-sample segment")
    train_end, validation_end = int(len(values) * train_ratio), int(len(values) * (train_ratio + validation_ratio))
    return WalkForwardSplit(values[:train_end], values[train_end:validation_end], values[validation_end:])
