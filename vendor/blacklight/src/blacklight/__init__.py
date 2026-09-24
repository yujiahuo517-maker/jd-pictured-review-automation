"""Blacklight —— 京东京喜采销/营销自动化工具包（skill + MCP）。

分层：
  core/    基建（登录态/HTTP/确认令牌/审计/配置），跨域共用，不依赖任何 domain
  osw/     采销工作台域（商品/毛利/定价/公共商品池认领/编辑商品）
  yx/      营销活动域（campaign/subsidy/markettool/bybt/ms）
  servers/ MCP 入口（blacklight-osw / blacklight-yx，工具名保留 osw_*/yx_*）
运行态（credentials/audit/浏览器数据）在 <skill根>/runtime，可用 BLACKLIGHT_HOME 覆盖。
"""
__version__ = "2.0"
