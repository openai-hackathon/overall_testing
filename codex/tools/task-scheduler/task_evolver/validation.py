import math


def finite_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError("expected a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError("expected a finite number") from exc
    if not math.isfinite(result):
        raise ValueError("expected a finite number")
    return result


def validate_pair_values(probability, weight):
    probability = finite_number(probability)
    weight = finite_number(weight)
    if not 0 <= probability <= 1:
        raise ValueError("probability must be between 0 and 1")
    if weight <= 0:
        raise ValueError("weight must be positive")
    return probability, weight
