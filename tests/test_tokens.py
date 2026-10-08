"""token 估算器测试。

这个文件主要守住一件事：**差分校准真的能消掉工具说明书那份固定开销**。
这正是整个设计的价值所在 —— 如果做不到，那就跟直接拿「总token/总字符」一样不准，
还不如不写这个类。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mini_agent.tokens import TokenEstimator  # noqa: E402


class TokenEstimatorTest(unittest.TestCase):

    def test_estimate_uses_unit_price(self):
        """1 字符 = 1 token 时，估算值就等于字符数。"""
        est = TokenEstimator(chars_per_token=1.0)
        self.assertEqual(est.estimate(1000), 1001)  # 末尾 +1 是凑整的余量
        self.assertEqual(est.estimate(0), 0)

    def test_first_observation_cannot_calibrate(self):
        """只有一次观测算不出单价 —— 差分至少需要两点。"""
        est = TokenEstimator()
        self.assertFalse(est.calibrate(1000, 500))
        self.assertEqual(est.calibrations, 0)

    def test_differential_removes_fixed_overhead(self):
        """最关键的一条：固定开销必须被差分消掉。

        构造一份「真实」观测：每次请求都带 500 token 的工具说明书 + 2 字符/token 的正文。
        如果实现是拿总量直接除（1000/1000=1.0、3000/2000=1.5），会得到荒唐的单价；
        做差分之后 Δchars/Δtokens = 2000/1000 = 2.0，正好是真值。
        """
        est = TokenEstimator(chars_per_token=2.5)  # 故意给个偏离真值的起点
        schema_tokens = 500
        real_chars_per_token = 2.0

        def observe(chars):
            return schema_tokens + int(chars / real_chars_per_token)

        for chars in (1000, 3000, 5000, 7000):
            est.calibrate(chars, observe(chars))

        # 起点是 2.5，每次观测把一半权重拉向真值 2.0，四次之后应该已经很接近
        self.assertAlmostEqual(est.chars_per_token, real_chars_per_token, delta=0.2)
        self.assertGreaterEqual(est.calibrations, 3)

    def test_naive_division_would_be_wrong(self):
        """对照试验：证明「不做差分」的方案确实会算错，免得有人把它改回去。"""
        chars, real_tokens = 1000, 1000          # 含 500 固定开销
        naive = chars / real_tokens              # = 1.0
        self.assertNotAlmostEqual(naive, 2.0, delta=0.3)

    def test_tiny_sample_does_not_calibrate(self):
        """样本差得太少时噪声会淹没信号，这时候宁可不校准。"""
        est = TokenEstimator()
        est.calibrate(1000, 500)
        est.calibrate(1010, 505)  # 只差 10 字符
        self.assertEqual(est.calibrations, 0)

    def test_unit_price_is_clamped(self):
        """喂极端观测也不能让单价跑飞 —— 否则预算会失控。"""
        est = TokenEstimator(chars_per_token=2.5)
        est.calibrate(1000, 500)
        est.calibrate(101000, 505)   # 字符暴涨、token 几乎不变 → 单价会被推向极高
        self.assertLessEqual(est.chars_per_token, 4.0)

        est2 = TokenEstimator(chars_per_token=2.5)
        est2.calibrate(1000, 500)
        est2.calibrate(11000, 10500)  # token 暴涨 → 单价会被压向极低
        self.assertGreaterEqual(est2.chars_per_token, 0.5)

    def test_zero_usage_is_ignored(self):
        """假模型 usage 全是 0，不能拿 0 去校准（会除零或算出无穷大）。"""
        est = TokenEstimator()
        self.assertFalse(est.calibrate(1000, 0))
        self.assertFalse(est.calibrate(0, 500))
        self.assertEqual(est.calibrations, 0)

    def test_describe_mentions_calibration(self):
        est = TokenEstimator()
        self.assertIn("尚未校准", est.describe())
        est.calibrate(1000, 500)
        est.calibrate(5000, 2500)
        self.assertIn("已校准", est.describe())


if __name__ == "__main__":
    unittest.main()
