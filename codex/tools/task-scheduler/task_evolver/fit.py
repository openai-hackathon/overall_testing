import math

from .validation import finite_number, validate_pair_values

TEMPERATURE = 2.0


def sigmoid(x):
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    exp_x = math.exp(x)
    return exp_x / (1.0 + exp_x)


def _loss(z, pairs, l2):
    total = 0.5 * l2 * sum(value * value for value in z.values())
    for pair in pairs:
        diff = z[pair.a_key] - z[pair.b_key]
        total += pair.weight * (
            max(diff, 0.0) + math.log1p(math.exp(-abs(diff))) - pair.p_a_wins * diff
        )
    return total


def fit_bradley_terry(pairs, ref_key, l2=0.1, lr=0.1, iters=2000):
    l2, lr = finite_number(l2), finite_number(lr)
    if l2 <= 0 or lr <= 0 or type(iters) is not int or iters <= 0:
        raise ValueError("l2, lr and iteration count must be positive")
    pairs = list(pairs)
    for pair in pairs:
        validate_pair_values(pair.p_a_wins, pair.weight)
    keys = sorted({p.a_key for p in pairs} | {p.b_key for p in pairs} | {ref_key})
    z = {k: 0.0 for k in keys}
    step = lr
    loss = _loss(z, pairs, l2)
    for iteration in range(iters + 1):
        grad = {k: l2 * z[k] for k in keys}
        for pair in pairs:
            err = sigmoid(z[pair.a_key] - z[pair.b_key]) - pair.p_a_wins
            grad[pair.a_key] += pair.weight * err
            grad[pair.b_key] -= pair.weight * err
        grad[ref_key] = 0.0
        if max(abs(value) for value in grad.values()) <= 1e-6:
            return z
        if iteration == iters:
            break
        norm = sum(value * value for value in grad.values())
        for _ in range(60):
            candidate = {key: z[key] - step * grad[key] for key in keys}
            candidate_loss = _loss(candidate, pairs, l2)
            if (
                math.isfinite(candidate_loss)
                and candidate_loss <= loss - 1e-4 * step * norm
            ):
                z, loss = candidate, candidate_loss
                step = min(step * 2, 1.0 / l2)
                break
            step *= 0.5
        else:
            raise RuntimeError("BT line search failed")
    raise RuntimeError("BT fit did not converge")


def importance_from_z(z, temperature=TEMPERATURE):
    temperature = finite_number(temperature)
    if temperature <= 0:
        raise ValueError("temperature must be positive")
    return {k: 100.0 * sigmoid(finite_number(v) / temperature) for k, v in z.items()}
