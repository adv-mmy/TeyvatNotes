"""指数退避与抖动计算。"""

import math

from teyvatnotes.worker.models import RetryPolicy


def calculate_retry_delay(
    policy: RetryPolicy,
    *,
    attempt_number: int,
    random_value: float,
) -> float:
    """返回当前 attempt 失败后的下次等待秒数。"""

    if attempt_number < 1:
        raise ValueError("attempt_number 必须大于 0")
    if not 0 <= random_value <= 1:
        raise ValueError("random_value 必须在 0 到 1 之间")

    try:
        exponential_delay = math.ldexp(policy.base_delay_seconds, attempt_number - 1)
    except OverflowError:
        exponential_delay = policy.max_delay_seconds
    capped_delay = min(exponential_delay, policy.max_delay_seconds)
    jitter_span = capped_delay * policy.jitter_ratio
    jittered_delay = capped_delay - jitter_span + (2 * jitter_span * random_value)
    return min(jittered_delay, policy.max_delay_seconds)
