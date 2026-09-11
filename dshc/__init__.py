"""M3-DSH DeepSeek Crypto Top - 公共模块.

时间约定
--------
项目内所有持久化时间统一使用 **UTC 毫秒时间戳**(int, 见 timeutil.utc_ms)。
对外展示/日志时统一通过 timeutil.fmt() 明确标注 "UTC" 或 "北京时间(UTC+8)"。
"""

__version__ = "0.1.0"
PROJECT_TAG = "m3dsc"
