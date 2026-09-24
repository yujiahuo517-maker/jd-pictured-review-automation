"""路径解析——三分：代码 / 运行态 / 决策配置。

- **代码**：包内，只读。
- **运行态** `<skill根>/runtime`：credentials / audit / 浏览器数据。机器相关、可再生、**不入库**。
  可用 BLACKLIGHT_HOME 覆盖整个目录，或单独用 *_FILE 覆盖某文件。兼容旧环境变量 YX_CRED_FILE / YX_AUDIT_LOG。
- **决策配置** `<skill根>/config`：protected.json(禁止触碰清单) 等**人工拍板、需要版本追溯**的文件。
  ★这类东西丢了没法再生，所以必须入库，不能跟 runtime 混在一起（2026-08-03 从 runtime 迁出）。
"""
import os

_CORE = os.path.dirname(os.path.abspath(__file__))                       # .../src/blacklight/core
_ROOT = os.path.abspath(os.path.join(_CORE, os.pardir, os.pardir, os.pardir))                     # <skill根>
_DEFAULT_HOME = os.path.join(_ROOT, "runtime")                           # <skill根>/runtime
_DEFAULT_CONFIG = os.path.join(_ROOT, "config")                          # <skill根>/config


def home() -> str:
    h = os.environ.get("BLACKLIGHT_HOME", "").strip() or _DEFAULT_HOME
    try:
        os.makedirs(h, exist_ok=True)
    except Exception:
        pass
    return h


def credentials_path() -> str:
    return (os.environ.get("YX_CRED_FILE", "").strip()
            or os.environ.get("BLACKLIGHT_CRED_FILE", "").strip()
            or os.path.join(home(), "credentials.json"))


def jx_auth_credentials_path() -> str:
    """**兜底登录源**：jx-auth skill 的凭证（同一 SSO 主票，2026-08-05 实证 6 个网关全通）。

    默认取同级目录 `<skills>/jx-auth/credentials.json`。这是本包唯一一处**向外部 skill 的路径依赖**，
    所以：①只当兜底、缺了不影响主流程；②可用 `JX_AUTH_CRED_FILE` 覆盖。"""
    env = os.environ.get("JX_AUTH_CRED_FILE", "").strip()
    if env:
        return env
    return os.path.abspath(os.path.join(_ROOT, os.pardir, "jx-auth", "credentials.json"))


def audit_path() -> str:
    return (os.environ.get("YX_AUDIT_LOG", "").strip()
            or os.environ.get("BLACKLIGHT_AUDIT_LOG", "").strip()
            or os.path.join(home(), "audit.log"))


def browser_data_dir() -> str:
    return os.path.join(home(), "login_browser_data")


def ledger_path() -> str:
    """广告分析账本（SKU 级历史快照）。放 runtime/ledger/ 且 **gitignore**——含 SKU 毛利/价格，
    属业务数据不入库（与 `_analysis_*` 同类）。

    ⚠️但它与 runtime 里其它东西不同：**不可再生**（历史快照丢了就没了，重跑不出来）。
    清理 runtime / 换机器前务必单独备份。BLACKLIGHT_LEDGER_FILE 可覆盖。"""
    env = os.environ.get("BLACKLIGHT_LEDGER_FILE", "").strip()
    if env:
        return env
    return os.path.join(home(), "ledger", "ad_ledger.csv")


def exports_dir(sub: str = "") -> str:
    """导出目录 `runtime/exports[/sub]`（可再生、不入库）：采集输入/输出表等落在这里。"""
    d = os.path.join(home(), "exports", sub) if sub else os.path.join(home(), "exports")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def review_collector_dir() -> str:
    """同品带图好评采集器目录（`vendor/jd_good_reviews`，同事的 Playwright 脚本原样收编）。
    BLACKLIGHT_REVIEW_COLLECTOR 可指到别处（比如仍想跑 Downloads 里那份）。"""
    env = os.environ.get("BLACKLIGHT_REVIEW_COLLECTOR", "").strip()
    if env:
        return env
    return os.path.join(_ROOT, "vendor", "jd_good_reviews")


def config_dir() -> str:
    """决策配置目录（入库）。BLACKLIGHT_CONFIG_DIR 可覆盖。"""
    d = os.environ.get("BLACKLIGHT_CONFIG_DIR", "").strip() or _DEFAULT_CONFIG
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def protected_path() -> str:
    """禁止触碰清单。优先 config/；若只在旧的 runtime/ 里存在，则**返回旧路径**由调用方迁移，
    避免升级瞬间把已拍板的规则读丢。"""
    env = os.environ.get("BLACKLIGHT_PROTECTED_FILE", "").strip()
    if env:
        return env
    new = os.path.join(config_dir(), "protected.json")
    if not os.path.exists(new):
        legacy = os.path.join(home(), "protected.json")
        if os.path.exists(legacy):
            return legacy
    return new
