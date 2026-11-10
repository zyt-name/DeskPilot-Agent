"""让无头内核在指纹上等于普通浏览器。

为什么必须做：`--headless` 会把 UA 写成 `HeadlessChrome/157.0.0.0`，并且把
`navigator.webdriver` 置成 `true`。很多站点（自如用的腾讯 EdgeOne）看到这两条
就直接返回「请求已被站点的安全策略拦截」，首页都进不去——真机实测，同一台机器、
同一个内核，只把这两处改回正常值，拦截页立刻消失。

边界：这里只抹掉「这是个自动化浏览器」这一条特征，不碰 Cookie、不改
`location`、不解验证码、不伪造登录态。登录该扫的码还是用户自己扫。
"""

# 注入到每个新文档（`Page.addScriptToEvaluateOnNewDocument`）里。
# 只改 `webdriver`：写在原型上，页面自己的检测读到的就是 undefined。
STEALTH_JS = """(() => {
  try {
    Object.defineProperty(Navigator.prototype, 'webdriver', {
      get: () => undefined,
      configurable: true,
    });
  } catch (e) {}
})();"""

__all__ = ["STEALTH_JS"]
