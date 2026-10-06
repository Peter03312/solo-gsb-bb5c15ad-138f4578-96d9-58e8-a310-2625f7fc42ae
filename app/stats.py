"""三角检验统计。

- binomial_tail：随机猜中概率 p=1/3 下答对次数的二项分布单侧上尾概率
  P(X >= k)，X ~ Binomial(n, 1/3)（对数空间 + 递推，避免大 n 时 comb() 中间溢出）。
- paired_binomial_p：复测配对对照的精确双侧配对二项检验（McNemar 精确检验）
  p 值，仅依据两类不一致对人数；p=1/2 时各项分母为 2 的幂，
  用 math.comb 精确整数求和后一次缩放，无递推舍入累积。
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


def paired_binomial_p(b: int, c: int) -> float:
    """精确双侧配对二项检验（McNemar 精确检验）p 值。

    仅依据两类不一致对人数 b、c（复测对照中「仅首场对」与「仅次场对」），
    n = b + c。原假设：两类等可能（p = 1/2）。
    双侧 p = min(1, 2 * P(X <= min(b, c)))，X ~ Binomial(n, 1/2)。
    n = 0（无不一致对）时按约定 p = 1。

    sum_{i=0}^{k} C(n,i) 以 math.comb 精确整数求和；除以 2^n（2 的幂）
    在正规浮点范围内不引入额外舍入，n 极大（下溢区）时 p 截断为 0。
    """
    n = b + c
    if n <= 0:
        return 1.0
    k = min(b, c)
    s = sum(math.comb(n, i) for i in range(k + 1))
    return min(1.0, 2.0 * (s / 2 ** n))
