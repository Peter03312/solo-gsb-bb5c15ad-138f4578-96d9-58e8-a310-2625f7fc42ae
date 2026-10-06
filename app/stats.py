"""三角检验统计。

- binomial_tail：随机猜中概率 p=1/3 下答对次数的二项分布单侧上尾概率
  P(X >= k)，X ~ Binomial(n, 1/3)。用对数空间 + 递推，避免大 n 时 comb() 中间溢出。
- paired_binomial_two_sided：同一批评员两场复测的配对二项检验（McNemar 精确检验）
  双侧 p 值——只看两场对错不一致的评员，在“变化方向各半”的原假设下做精确二项推断。
"""

import math

P = 1.0 / 3.0
Q = 2.0 / 3.0


def binomial_tail(n: int, k: int) -> float:
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    # 先算最大项（尾区内），再向上、向下递推求和，防止下溢丢失。
    log_c = math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)
    log_t = log_c + k * math.log(P) + (n - k) * math.log(Q)
    # 首项已小于浮点可表示量级时，整个尾概率必然也下溢为 0
    if log_t < -745.0:
        return 0.0
    total = math.exp(log_t)
    # T_{i+1} / T_i = (n-i)/(i+1) * p/q
    # i = k..n 各项（T_k 已计入）
    i = k
    cur = math.exp(log_t)
    while i < n:
        cur *= (n - i) / (i + 1) * (P / Q)
        total += cur
        i += 1
    # 极小尾概率截断到 0
    return min(1.0, max(0.0, total))


def paired_binomial_two_sided(n_disagree: int, k: int) -> float:
    """配对二项检验（McNemar 精确检验）双侧 p 值。

    仅两场答对/答错不一致的评员参与：n_disagree = 仅首场对 + 仅次场对，
    k = 其中“仅首场对”的人数。原假设 H0：变化方向等概率（p=1/2），
    于是 K ~ Binomial(n_disagree, 1/2)，双侧 p 值为
    2 * P(K ≥ max(k, n_disagree−k))，并截断到 1。

    - n_disagree = 0：无差异评员，调用方按约定直接取 p=1（此函数亦返回 1）。
    - n_disagree ≥ 1：用 math.comb 精确求和；名单上限 500，中间值不溢出。
    """
    if n_disagree <= 0:
        return 1.0
    k = max(0, min(k, n_disagree))
    tail_k = max(k, n_disagree - k)  # 取远离 1/2 的一侧，乘 2 即双侧
    tail = sum(
        math.comb(n_disagree, i) for i in range(tail_k, n_disagree + 1)
    ) / (2 ** n_disagree)
    return min(1.0, 2.0 * tail)
