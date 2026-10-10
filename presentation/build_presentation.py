"""Build the demo scripts: the whole run sheet and one page per presenter.

    venv\\Scripts\\python.exe presentation\\build_presentation.py

Writes presentation/run-sheet.html (everyone, with the projector aids) and
presentation/team/<n>-<name>.html (one presenter's lines, cue and rehearsal notes).
Each presenter speaks once, in order; the project lead opens and does not return.
"""

from __future__ import annotations

from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parent

PEOPLE = {
    "fw": {"name": "Fengwei Zhang", "role": "Project Lead", "short": "Fengwei"},
    "tg": {"name": "Tianshuo Gao", "role": "Interface Developer", "short": "Tianshuo"},
    "xz": {"name": "Xiao Zhang", "role": "Agent Developer & Test", "short": "Xiao"},
    "gz": {"name": "Gang Zhao", "role": "Data Engineer", "short": "Gang"},
    "wz": {"name": "Wenhan Zhang", "role": "Agent Developer & Test", "short": "Wenhan"},
}
ORDER = ["fw", "tg", "xz", "gz", "wz"]

# (speaker, start, length, title, beats); a beat is (say, show). HTML is allowed in "show".
SEGMENTS = [
    ("fw", "0:00", "1:15", "开场：问题与目标", [
        ("Good morning everyone. We are the Bargain Agent team. I'm Fengwei Zhang, the project lead, and with me are Gang Zhao, Tianshuo Gao, Xiao Zhang and Wenhan Zhang.",
         "App 的 Find 页全屏显示（浏览器缩放 125%）。念到名字时每人点头示意。"),
        ("Here is the problem we started from. When you want to buy something online in New Zealand, a pair of headphones or an air fryer, the price changes every week and it is different at every shop. Most of us check PB Tech, Noel Leeming and Mighty Ape by hand, again and again, and we still miss the sale.",
         "停在 Find 页，不操作。"),
        ("Price alert tools exist, but you have to find the exact product page first and fill in a form. Our goal was an agent you can simply talk to. You describe what you want in your own words, in English or Chinese. The agent finds the product, compares it across shops, keeps watching the price, and emails you when it becomes a real deal.",
         "指一下 Find 页的输入框和下面的中文示例。"),
        ("I will explain how it is built and how accurate it is, and then my teammates will show it working live with real data.",
         "准备切到架构图。"),
    ]),
    ("fw", "1:15", "2:30", "架构与关键设计决策", [
        ("Let me start with the design, because the system you will see today is quite different from our proposal.",
         "切到演示稿底部的“架构图”。"),
        ("In the proposal we planned to scrape retailer websites directly, with BeautifulSoup and Selenium. When we built it, the big retailers blocked our scraper again and again, and every site needed its own parser that broke whenever the page changed. It did not scale.",
         "不操作。"),
        ("So I redesigned the system around two ideas. The first is data. Instead of scraping each shop, we read Google Shopping New Zealand through search APIs: Serper as the main provider and SerpApi as the backup. One search returns structured listings from every merchant at once, with the title, the shop and the price. We only read a shop's own page when a user pastes a specific link, and even then through a chain of three page-reading services.",
         "指图中的 Search 方框。"),
        ("The second idea is intelligence. The project is now a multi-agent pipeline built with LangGraph, with eight AI agents, each with one job. In Find, the Understand agent turns your sentence into a structured need. The Rewrite agent writes search queries. The Select agent picks the real products, and the Describe agent explains each pick. If nothing suitable is found, the graph loops back and tries new search terms once.",
         "依次指 Understand → Rewrite → Search → Select → Describe，最后指 “retry once” 回环箭头。"),
        ("The most important rule in our design is: agents choose, code decides. An agent can only pick listings by the IDs our server assigned, so it cannot invent a product or a price. Every model output is validated. Budgets, deal thresholds, duplicate checks and whether an email may be sent are all decided by plain code. And if the model is unavailable, every agent falls back to a rule-based version, so the app keeps working.",
         "指图右侧的 Offer-verifier（veto only）和 “one email per episode”。"),
    ]),
    ("fw", "3:45", "2:05", "真实数据评估", [
        ("Before we show it live, here is how well it works. Our proposal promised three accuracy measures: price extraction, instruction understanding and alert triggering. We tested all three with real data.",
         "切到演示稿底部的“评估结果”表。"),
        ("First, real price accuracy. We took seventeen common products, found each one at a trusted New Zealand shop, opened the shop's own page and compared the prices. The first run matched only nine out of fifteen, sixty percent. I analysed every miss, and the causes were very specific: superscript cents read as forty-four thousand eight hundred dollars, an Afterpay instalment taken as the price, a recommended retail price instead of the sale price, and a price before GST. After fixing each cause, by using the page's structured data first and tightening the prompts, the re-run matched fourteen out of fourteen exactly.",
         "指 60% 和 100% 两行。"),
        ("With the real language model, the price agent read thirteen out of thirteen pages correctly and reported a false price zero times, including on a page that tried to inject instructions. Instruction parsing got fifteen of sixteen commands fully right, and ninety-eight percent of all fields. In ten alert scenarios, the scheduler sent every email it should and no email it should not: precision and recall of one point zero.",
         "依次指 13/13、15/16、1.0。"),
    ]),
    ("fw", "5:50", "0:55", "工程与交付", [
        ("Two engineering points. The first is cost and safety: a daily limit per provider, at most five merchants per product, one search per check, and a server that only listens on your own computer, with CSRF and origin checks. The second is delivery. I packaged the system as a Windows program: you unzip it, double-click BargainAgent dot exe, and it opens in the browser, with no Python needed. There is also a source version that installs itself on first run.",
         "（可选）打开文件资源管理器，展示 <code>BargainAgent-windows.zip</code> 和解压后的 <code>BargainAgent.exe</code>。"),
        ("That is the design and the results. Now let's see it working with real data. Tianshuo, over to you.",
         "切到 App 的 Find 页。<span class=\"handoff\">交接 → Tianshuo。</span>"),
    ]),
    ("tg", "6:45", "2:00", "现场演示：用一句话找商品", [
        ("Thanks. This is the web interface. It runs locally, on your own computer, in the browser.",
         "Find 页。"),
        ("I will ask for something real: noise cancelling headphones under four hundred New Zealand dollars.",
         "点示例 <b>Noise cancelling headphones under NZD 400</b>，再点 <b>Find with AI agents</b>。<span class=\"cue\">点完马上接着讲，不要等结果。</span>"),
        ("While it runs, you can watch the progress bar. Each step is one agent at work: Understand, Search terms, Search, Choose and Summarise. A real search takes about thirty to fifty seconds, because it calls real search providers and a real language model.",
         "指进度条上正在亮的那一步。"),
        ("The interface is designed for that wait. You always see which stage is working, and the results are saved, so if you open a shop's page and come back, they are still here.",
         "继续等结果。"),
        ("Here are the recommendations. Each card shows the product, the shop, whether the shop is on our trusted list, the price in New Zealand dollars, and a short summary written by the Describe agent. Above the cards are the actual search terms the Rewrite agent created.",
         "结果出来后慢慢往下滚一张卡片；依次指 Trusted 标签、价格、摘要，最后指上方带放大镜的搜索词。"),
        ("Now the part I like most: you can refine in conversation, like talking to a shop assistant. I will say: only trusted New Zealand shops.",
         "点 Refine 区的 <b>Only trusted NZ shops</b>，再点 <b>Refine</b>。"),
        ("The agents keep the earlier results and search again with the new condition. While that runs, Xiao will explain what the agents just did.",
         "<span class=\"handoff\">交接 → Xiao（细化正在跑，不要等）。</span>"),
    ]),
    ("xz", "8:45", "1:45", "智能体在背后做了什么，以及同款匹配", [
        ("Thank you. Behind those cards, four agents worked in sequence.",
         "不操作，等细化结果。"),
        ("The Understand agent read the sentence and extracted the category, the budget of four hundred dollars and the condition, new. The Rewrite agent then wrote several search queries, the ones you saw next to the magnifying glass, and those searches run in parallel.",
         "指上方的搜索词。"),
        ("The Select agent receives at most thirty listings and picks up to eight. It has to follow hard rules: no accessories, no second-hand items unless you ask for them, and nothing over budget. And because it can only answer with listing IDs, it cannot make up a product.",
         "不操作。"),
        ("The refined results are back, and they now come only from trusted shops.",
         "细化结果出来后，指几张卡片上的 Trusted 标签。"),
        ("Next is monitoring. When I click Compare and monitor, the system looks for the same product at other merchants. Rules filter by brand, model and price range, and then the offer-verifier agent checks each candidate. It can only reject a candidate, never add one. Here you can see the shops selling this model, and any same-product match is marked \"AI: same product\". Trusted shops are ticked by default, and we watch at most five merchants per product to keep the cost under control.",
         "点第一张卡片的 <b>Compare &amp; monitor</b>；在弹出面板里指 Sellers，有 “Same product at other merchants” 的话再指 “AI: same product” 标签和勾选框。"),
        ("I set my target price and create the monitor. Gang will show what happens next.",
         "把 Target 改成比现价低一些（避免现场发邮件），点 <b>Create monitor</b>。<span class=\"handoff\">交接 → Gang。</span>"),
    ]),
    ("gz", "10:30", "1:45", "数据：价格从哪来，存在哪里", [
        ("Thanks. We are now on the Monitors page, running on our demo dataset. Real monitoring needs weeks to build up history, so for this presentation the earlier price points are simulated around real current prices, as the banner at the top says. The products, shops and current prices are real. Here is a Fitbit Charge 6, watched at two shops.",
         "点顶部 <b>Monitors</b>；先指顶部黄色的 “Demo data” 提示条，再指 Fitbit 卡片上的 BEST NOW 和 ALERT。"),
        ("Each check runs one shared Google Shopping search for the whole monitor, however many shops it watches, and reads every merchant's current price by Google's product ID. If a shop is missing from the results, we do at most two detail lookups. So one check costs about one search.",
         "不操作。"),
        ("Let me open the details.",
         "点 Fitbit 卡片上的 <b>Details</b>。"),
        ("Every price is stored in a local SQLite database, so we can show history. This chart shows the price at each shop over time, with the target as a dashed line. Below it, each merchant shows its latest price and when it was checked.",
         "指走势图、红色虚线目标价，然后指下面的两行商家。"),
        ("From that history we compute the thirty-day low and average, so you know whether today's price is actually good. You can also enter shipping for each shop and compare delivered prices, item plus shipping, because the cheapest item is not always the cheapest at your door.",
         "指标题下方的 30-day low / avg；再指 Shipping 输入框和 “Compare delivered prices” 勾选项。"),
        ("Finally, every provider has a daily call limit, so the app can never run up a big bill. Wenhan will now cover alerts and testing.",
         "指右上角的 Serper 每日次数。<span class=\"handoff\">交接 → Wenhan。</span>"),
    ]),
    ("wz", "12:15", "1:45", "提醒规则、邮件与测试", [
        ("Thanks. The last step of the agent loop is the alert.",
         "详情抽屉往下滚到 ALERT SETTINGS。"),
        ("Whether something is a deal is decided by code, not by the model. It happens when the cheapest same-product offer from a trusted shop reaches your target price, or, if you choose, when the price drops by a set percentage. Offers from unknown or overseas sellers are shown, but they never trigger an email.",
         "指 When price is / Target / Or drop % 三个设置。"),
        ("Here is a real alert email the system sent.",
         "切到 Gmail 标签页，打开提前准备好的提醒邮件（标题以 [Bargain Agent] 开头）。"),
        ("The subject tells you the product, the price and the shop. Inside you get the price, the thirty-day context, the shop link and a short summary from the deal-summary agent. One price drop sends one email. If the next check finds the same deal again, you are not spammed.",
         "依次指邮件标题、价格那一行、30 天最低/平均、商家链接。"),
        ("To make sure all of this behaves correctly, we have three hundred and fifty-eight automated tests. They run offline in about two minutes and cover every agent, the data providers using recorded responses, the scheduler, the database migrations and the web API.",
         "切到终端窗口，展示提前跑好的 <code>Ran 358 tests … OK</code>。"),
    ]),
    ("wz", "14:00", "1:00", "局限与总结", [
        ("Before we finish, we want to be clear about the limits. We only see what Google Shopping New Zealand lists, so we do not claim to find the lowest price on the whole web. Our real price sample was seventeen products, slightly below the twenty we planned, because of API quotas, and a one-week continuous monitoring test is our next step. Future work includes more channels such as Telegram, and a cloud version so monitoring keeps running when your computer is off.",
         "切回 App 的 Monitors 页。"),
        ("To sum up: Bargain Agent turns one sentence into a product that is watched across several New Zealand shops. Eight agents do the understanding and the choosing. Code makes every decision that costs money or sends a message. And our real-data tests show that it is accurate.",
         "停在 Monitors 页。"),
        ("Thank you for listening. We are happy to take your questions.",
         "全组站好，进入提问。"),
    ]),
]

QA = {
    "scrape": ("Why didn't you scrape the retailer websites as planned?",
               "Big retailers blocked our scraper, and every site needed its own fragile parser. Google Shopping through search APIs gives structured prices from every New Zealand merchant in one call. We still read shop pages for pasted links, through three fallback services."),
    "invent": ("How do you stop the AI from making up products or prices?",
               "Agents can only return IDs of listings our server actually saw. A price read from a page must appear on that page word for word. Every output is validated, and all decisions about money and emails are made in code."),
    "down": ("What happens if the AI or a provider is down?",
             "Search falls back from Serper to SerpApi. Every agent has a rule-based fallback, so Find still returns results, just ranked by price and merchant. The page shows \"Some agents used fallbacks\" so the user knows."),
    "trusted": ("Why do only trusted merchants trigger alerts?",
                "Unknown marketplace and overseas sellers can be scams, ship slowly, or list converted prices. We still show them for comparison, but an email should only go out for a deal you can safely buy."),
    "same": ("How do you know two shops sell the same product?",
             "Rules first: brand and model words must match, and the price must be within 50 to 150 percent of the chosen product, which removes accessories and bundles. Then the offer-verifier agent can reject any remaining mismatch. The user makes the final choice in the preview."),
    "cost": ("How much does it cost to run?",
             "One check is about one search, and every provider has a daily limit we set, so the cost stays small and predictable. Serper's free tier alone covers about 1,250 searches."),
    "history": ("Is the price history real?",
                "The products, shops and current prices are real, from Google Shopping. The earlier points in the demo database are simulated, because a month of real history takes a month to collect. In normal use every point on the chart is a real check, and \"Check now\" in the demo is live."),
    "off": ("Does it work when the computer is off?",
            "Not yet. With the computer on, a Windows scheduled task checks due monitors without the browser open. A cloud version is future work."),
    "different": ("How is this different from existing price comparison sites?",
                  "You describe what you want in natural language instead of searching for an exact product. The agents choose real products for your budget and conditions, match the same product across shops, and alert you based on your own target, with price history and delivered-price comparison."),
}
QA_FOR = {"tg": ["different", "down"], "xz": ["invent", "same"], "gz": ["history", "cost", "scrape"],
          "wz": ["trusted", "off"]}

REHEARSE = {
    "tg": ["双击“演示模式.bat”，打开后在 Find 页按台词操作一遍。搜索要 30–50 秒，台词就是为这段等待设计的，点完马上接着讲。",
           "细化（Refine）点完就交给 Xiao，不用等结果。",
           "搜索要联网，而且用的是共享额度，每天排练别超过 10 次。"],
    "xz": ["你开口时 Tianshuo 的细化还在跑，前三段台词不需要看结果。",
           "点第一张卡片的 Compare &amp; monitor。有的商品只有一家店，面板里就只显示 Sellers 一行，照常讲即可。",
           "Create monitor 前把 Target 改得比现价低。排练时建的多余监控可以先 Stop 再 Delete。"],
    "gz": ["Monitors 页里打开 <b>Fitbit Charge 6</b> 的 Details（它有两家店、两条线）。",
           "页面顶部的黄色 “Demo data” 提示是故意保留的，你的第一段台词就是在解释它。",
           "如果走势图的日期不是到今天，双击“重置演示数据.bat”再打开。"],
    "wz": ["提醒邮件在组长的 Gmail 里，正式演示时用组长的电脑展示；排练时只练台词。",
           "测试结果（Ran 358 tests … OK）也是正式演示时在组长电脑的终端里展示。",
           "你是最后一个，讲完 “Thank you for listening…” 后全组一起进入提问。"],
}


CSS = """
:root {
  --paper: #f6f7f9; --card: #ffffff; --ink: #18212b; --muted: #5b6673; --line: #dfe3e8; --accent: #1d5f8c; --accent-soft: #e4eef6; --warn: #b2432f;
  --fw: #1d5f8c; --gz: #8a5a00; --tg: #2f7d4f; --xz: #8b3b78; --wz: #b2432f;
  --font-body: "IBM Plex Sans", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
  --font-label: "IBM Plex Sans Condensed", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
  --font-mono: "IBM Plex Mono", ui-monospace, Consolas, monospace;
}
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --paper: #11161c; --card: #181f27; --ink: #e6ebf0; --muted: #9aa6b2; --line: #2a333d; --accent: #7db7e3; --accent-soft: #1b2d3c; --warn: #f08c76;
  --fw: #7db7e3; --gz: #e0b453; --tg: #6fcf97; --xz: #e08fd0; --wz: #f08c76; color-scheme: dark } }
:root[data-theme="dark"] {
  --paper: #11161c; --card: #181f27; --ink: #e6ebf0; --muted: #9aa6b2; --line: #2a333d; --accent: #7db7e3; --accent-soft: #1b2d3c; --warn: #f08c76;
  --fw: #7db7e3; --gz: #e0b453; --tg: #6fcf97; --xz: #e08fd0; --wz: #f08c76; color-scheme: dark }
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font-family: var(--font-body); font-size: 15px; line-height: 1.6; }
.wrap { max-width: 1080px; margin: 0 auto; padding-inline: 20px; padding-block: 32px 64px; display: grid; gap: 28px; }
h1, h2, h3 { text-wrap: balance; line-height: 1.25; margin: 0; }
h1 { font-size: clamp(26px, 4vw, 34px); font-weight: 700; letter-spacing: -0.01em; }
h2 { font-size: 20px; font-weight: 700; }
h3 { font-size: 16px; font-weight: 600; }
p { margin: 0; }
.label { font-family: var(--font-label); text-transform: uppercase; letter-spacing: .06em; font-size: 12px; font-weight: 600; color: var(--muted); }
.mono { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
code { font-family: var(--font-mono); font-size: .9em; background: var(--accent-soft); padding: 1px 5px; border-radius: 4px; overflow-wrap: anywhere; }
header { display: grid; gap: 10px; }
.lede { color: var(--muted); max-width: 70ch; }
.panel { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 20px; display: grid; gap: 14px; min-width: 0; }
.cast { width: 100%; border-collapse: collapse; font-size: 14px; }
.cast th, .cast td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
.cast th { font-family: var(--font-label); font-weight: 600; color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
.cast tr:last-child td { border-bottom: 0; }
.cast tr.me td { background: var(--accent-soft); font-weight: 600; }
.scroll { overflow-x: auto; }
.who { display: inline-flex; align-items: center; gap: 7px; font-weight: 600; white-space: nowrap; }
.who::before { content: ""; width: 10px; height: 10px; border-radius: 50%; background: var(--c); flex: none; }
[data-s="fw"] { --c: var(--fw); } [data-s="gz"] { --c: var(--gz); } [data-s="tg"] { --c: var(--tg); } [data-s="xz"] { --c: var(--xz); } [data-s="wz"] { --c: var(--wz); }
.bar { display: flex; height: 14px; border-radius: 7px; overflow: hidden; border: 1px solid var(--line); }
.bar span { background: var(--c); }
.bar-legend { display: flex; justify-content: space-between; font-size: 12px; color: var(--muted); }
.filter { position: sticky; top: env(safe-area-inset-top, 0px); z-index: 5; background: var(--paper); padding-block: 10px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; border-bottom: 1px solid var(--line); }
.filter button { font: 600 13px var(--font-body); color: var(--ink); background: var(--card); border: 1px solid var(--line); border-radius: 999px; padding: 6px 12px; cursor: pointer; display: inline-flex; gap: 7px; align-items: center; }
.filter button[data-s]::before { content: ""; width: 9px; height: 9px; border-radius: 50%; background: var(--c); }
.filter button[aria-pressed="true"] { background: var(--ink); color: var(--paper); border-color: var(--ink); }
.filter button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.segments { display: grid; gap: 18px; }
.seg { display: grid; grid-template-columns: 92px minmax(0, 1fr); gap: 0 18px; }
.seg[hidden] { display: none !important; }
.rail { border-right: 3px solid var(--c); padding-right: 12px; text-align: right; display: grid; align-content: start; gap: 2px; }
.rail .t { font-family: var(--font-mono); font-size: 15px; font-weight: 500; }
.rail .d { font-size: 12px; color: var(--muted); }
.seg-body { background: var(--card); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; min-width: 0; }
.seg-head { padding: 14px 18px; border-bottom: 1px solid var(--line); display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: baseline; }
.seg-head .who { color: var(--c); }
.beat, .cols-head { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr); border-bottom: 1px solid var(--line); }
.beat:last-child { border-bottom: 0; }
.say, .show { padding: 12px 18px; min-width: 0; }
.say { font-size: 15.5px; }
.show { background: var(--accent-soft); font-size: 14px; border-left: 1px solid var(--line); }
.show::before { content: "屏幕"; display: block; font-family: var(--font-label); font-size: 11px; letter-spacing: .06em; color: var(--muted); font-weight: 600; margin-bottom: 2px; }
.cols-head div { padding: 7px 18px; }
.cols-head div + div { border-left: 1px solid var(--line); }
.cue { font-weight: 700; color: var(--warn); }
.handoff { font-style: italic; color: var(--muted); }
.cueline { border-left: 3px solid var(--c); padding: 8px 14px; background: var(--card); font-style: italic; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 18px; }
ol, ul { margin: 0; padding-left: 20px; display: grid; gap: 6px; }
.qa { display: grid; gap: 12px; }
.qa div { display: grid; gap: 3px; padding-bottom: 12px; border-bottom: 1px solid var(--line); }
.qa div:last-child { border-bottom: 0; padding-bottom: 0; }
.qa .q { font-weight: 600; }
.eval td.num { font-family: var(--font-mono); font-variant-numeric: tabular-nums; white-space: nowrap; }
pre.mermaid { margin: 0; background: transparent; text-align: center; }
.diagram { overflow-x: auto; }
.note { font-size: 13px; color: var(--muted); }
.facts { display: flex; flex-wrap: wrap; gap: 10px 28px; }
.facts div { display: grid; gap: 2px; }
.facts b { font-size: 18px; }
@media (max-width: 720px) {
  .seg { grid-template-columns: 1fr; gap: 6px; }
  .rail { border-right: 0; border-left: 3px solid var(--c); padding: 0 0 0 10px; text-align: left; grid-auto-flow: column; justify-content: start; gap: 10px; align-items: baseline; }
  .beat, .cols-head { grid-template-columns: 1fr; }
  .show { border-left: 0; border-top: 1px dashed var(--line); }
  .cols-head { display: none; }
}
"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@500'
         '&family=IBM+Plex+Sans+Condensed:wght@500;600&family=IBM+Plex+Sans:ital,wght@0,400;0,600;0,700;1,400&display=swap">')


def seconds(t: str) -> int:
    m, s = t.split(":")
    return int(m) * 60 + int(s)


def clock(total: int) -> str:
    return f"{total // 60}:{total % 60:02d}"


def spans() -> dict[str, tuple[str, str]]:
    out = {}
    for who in ORDER:
        segs = [s for s in SEGMENTS if s[0] == who]
        start = seconds(segs[0][1])
        end = seconds(segs[-1][1]) + seconds(segs[-1][2])
        out[who] = (clock(start), clock(end))
    return out


TOPICS = {"fw": "项目目的、架构与设计决策、真实数据评估、工程与打包", "tg": "用户界面、现场搜索、对话式细化",
          "xz": "智能体流水线、同款匹配、建立监控", "gz": "数据来源、价格数据库、价格历史",
          "wz": "提醒规则、邮件、自动化测试、局限与总结"}
SHOWS = {"fw": "架构图、评估结果表", "tg": "Find 页", "xz": "搜索词、Compare &amp; monitor 面板",
         "gz": "Monitors 页、Fitbit 详情", "wz": "提醒设置、Gmail、终端、Monitors 页"}


def cast_table(me: str | None = None) -> str:
    rows = []
    for n, who in enumerate(ORDER, 1):
        start, end = spans()[who]
        p = PEOPLE[who]
        rows.append(f'<tr class="{"me" if who == me else ""}"><td class="mono">{n}</td><td><span class="who" data-s="{who}">{escape(p["name"])}</span></td>'
                    f'<td>{escape(p["role"])}</td><td class="mono">{start}–{end}</td><td>{TOPICS[who]}</td><td>{SHOWS[who]}</td></tr>')
    return ('<div class="scroll"><table class="cast"><thead><tr><th>#</th><th>谁</th><th>角色</th><th>时间</th><th>讲什么</th><th>操作什么</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def bar() -> str:
    parts = "".join(f'<span data-s="{who}" style="flex: {seconds(spans()[who][1]) - seconds(spans()[who][0])}"></span>' for who in ORDER)
    marks = "".join(f"<span>{spans()[who][0]}</span>" for who in ORDER) + "<span>15:00</span>"
    label = ", ".join(f"{PEOPLE[w]['short']} {spans()[w][0]} to {spans()[w][1]}" for w in ORDER)
    return f'<div class="bar" role="img" aria-label="{label}">{parts}</div><div class="bar-legend mono">{marks}</div>'


def segment(seg) -> str:
    who, start, length, title, beats = seg
    body = "".join(f'<div class="beat"><p class="say">{escape(say)}</p><p class="show">{show}</p></div>' for say, show in beats)
    return (f'<article class="seg" data-s="{who}"><div class="rail"><span class="t">{start}</span><span class="d">{length}</span></div>'
            f'<div class="seg-body"><div class="seg-head"><span class="who">{escape(PEOPLE[who]["name"])}</span><h3>{escape(title)}</h3></div>'
            f'<div class="cols-head"><div class="label">说（英文）</div><div class="label">屏幕上做什么</div></div>{body}</div></article>')


def qa(keys) -> str:
    return '<div class="qa">' + "".join(f'<div><p class="q">{escape(QA[k][0])}</p><p class="a">{escape(QA[k][1])}</p></div>' for k in keys) + "</div>"


def page(title: str, body: str, script: str = "", fragment: bool = False) -> str:
    if fragment:  # for an Artifact page: the host adds the document skeleton
        return f'<title>{escape(title)}</title>\n{FONTS}\n<style>{CSS}</style>\n<div class="wrap">{body}</div>\n{script}\n'
    return f"<!doctype html>\n<html lang=\"zh\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n<title>{escape(title)}</title>\n{FONTS}\n<style>{CSS}</style></head>\n<body>\n<div class=\"wrap\">{body}</div>\n{script}</body></html>\n"


ARCH = """<section class="panel" aria-labelledby="arch-h"><h2 id="arch-h">架构图（投屏用）</h2><div class="diagram"><pre class="mermaid">
flowchart LR
    D[Description] --> U[Understand agent] --> R[Rewrite agent] --> S[Search<br/>Google Shopping NZ<br/>Serper, SerpApi fallback]
    S --> Sel[Select agent] -->|nothing suitable, retry once| R
    Sel --> Desc[Describe agent] --> P[Same product at<br/>other merchants] --> C[User confirms]
    C --> T[(SQLite<br/>up to 5 merchants)]
    T --> M[Scheduler] --> O[One shared search per check]
    O --> V[Offer-verifier agent<br/>veto only] --> N[Deal at a trusted shop?<br/>one email per episode]
</pre></div></section>"""

EVAL = """<section class="panel" aria-labelledby="eval-h"><h2 id="eval-h">评估结果（投屏用）</h2><div class="scroll"><table class="cast eval">
<thead><tr><th>What we measured</th><th>Data</th><th>Result</th></tr></thead><tbody>
<tr><td>Real price accuracy, first run</td><td>15 products, 5 NZ shops</td><td class="num">9/15 exact · 60%</td></tr>
<tr><td>Real price accuracy, after fixes</td><td>14 products</td><td class="num">14/14 exact · 100%</td></tr>
<tr><td>Price agent on shop pages</td><td>13 pages, real model</td><td class="num">13/13 · false price 0</td></tr>
<tr><td>Instruction parsing</td><td>16 commands, real model</td><td class="num">15/16 · 98.1% of fields</td></tr>
<tr><td>Alert triggering</td><td>10 scenarios, 19 steps</td><td class="num">precision 1.0 · recall 1.0</td></tr>
<tr><td>Automated tests</td><td>offline suite</td><td class="num">358 passing</td></tr>
</tbody></table></div><p class="note">来源：docs/EVALUATION.md（2026-10-05 的真实运行），模型为 deepseek-flash。</p></section>"""

PREP = """<div class="grid2"><section class="panel" aria-labelledby="prep-h"><h2 id="prep-h">演示前准备（组长电脑）</h2><ol>
<li><b>用演示模式启动</b>：双击 <code>start-demo.bat</code>（打包版是“演示模式.bat”）。想让价格历史截止到当天，先运行 <code>start-demo.bat reset</code>。里面有 4 个带 30 天价格历史的监控，页面顶部的 “Demo data” 提示在 Gang 的台词里有说明。</li>
<li><b>准备提醒邮件</b>：Gmail 里搜 “Bargain Agent”，找到之前收到的提醒邮件并加星标。找不到的话，演示前把某个监控的目标价改到现价以上，点 Check now 收一封真邮件，再把目标价改回去。现场不触发邮件。</li>
<li><b>当天早上</b>跑一次 “Noise cancelling headphones under NZD 400”，确认 key 和额度正常。</li>
<li>终端里提前运行 <code>.\\venv\\Scripts\\python.exe -m unittest discover -s tests</code>，保留 “Ran 358 tests … OK” 的窗口。</li>
<li>浏览器标签按顺序开好：① 本页（架构图、评估表）② App <code>http://127.0.0.1:8765/</code> ③ Gmail 提醒邮件 ④ 终端。缩放 125%，关掉通知。</li>
<li>完整排练两遍并计时，每人控制在 ±15 秒内。</li>
</ol></section>
<section class="panel" aria-labelledby="backup-h"><h2 id="backup-h">出问题时怎么办</h2><ul>
<li><b>搜索超过 60 秒还没出结果</b>：说 <i>"The live search is slow today, so here are the results from this morning,"</i> 然后刷新页面，上一次的结果会自动恢复。</li>
<li><b>细化太慢</b>：Xiao 讲完前三段就直接点第一张卡片的 Compare &amp; monitor。</li>
<li><b>网络完全断了</b>：播放提前录好的演示视频（Win + G 录屏），建议无论如何都录一份。</li>
<li><b>Compare &amp; monitor 提示 “expired”</b>：服务器中途重启过，重新点一次 Find。</li>
<li><b>超时</b>：先删组长“工程与交付”的第一段，其次删 Gang 的第二段。</li>
</ul></section></div>"""


def run_sheet(fragment: bool = False) -> str:
    buttons = '<button type="button" id="f-all" aria-pressed="true" data-f="all">全部</button>' + "".join(
        f'<button type="button" id="f-{w}" aria-pressed="false" data-f="{w}" data-s="{w}">{PEOPLE[w]["short"]}</button>' for w in ORDER)
    body = f"""<header><span class="label">159.333 Computer Science Project · Final demo</span><h1>Bargain Agent 演示稿（15 分钟）</h1>
<p class="lede">每人讲一段，按顺序接力：组长先讲（目的、架构、评估、工程），之后 Tianshuo、Xiao、Gang 现场演示，Wenhan 讲提醒与测试并收尾。演讲词用英文照读，右边是讲到这一句时屏幕上做什么。</p></header>
<section class="panel" aria-labelledby="cast-h"><h2 id="cast-h">顺序与时间</h2>{bar()}{cast_table()}
<p class="note">建议全程由一个人坐在电脑前操作，讲的人站着讲；或者每人讲到自己时自己操作。提前定好。</p></section>
<nav class="filter" aria-label="按人筛选"><span class="label">只看</span>{buttons}</nav>
<section class="segments" aria-label="演讲稿">{"".join(segment(s) for s in SEGMENTS)}</section>
{PREP}
<section class="panel" aria-labelledby="qa-h"><h2 id="qa-h">可能被问到的问题</h2><p class="note">默认由组长回答；涉及某人那段内容的，由那个人先答，组长补充。</p>{qa(QA.keys())}</section>
{ARCH}{EVAL}"""
    script = """<script>
(function () {
  var buttons = Array.prototype.slice.call(document.querySelectorAll('.filter button'));
  var segs = Array.prototype.slice.call(document.querySelectorAll('.seg'));
  function apply(f) {
    buttons.forEach(function (b) { b.setAttribute('aria-pressed', String(b.dataset.f === f)); });
    segs.forEach(function (s) { s.hidden = f !== 'all' && s.dataset.s !== f; });
    try { localStorage.setItem('run-sheet-filter', f); } catch (e) {}
  }
  buttons.forEach(function (b) { b.addEventListener('click', function () { apply(b.dataset.f); }); });
  var saved = 'all';
  try { saved = localStorage.getItem('run-sheet-filter') || 'all'; } catch (e) {}
  if (!buttons.some(function (b) { return b.dataset.f === saved; })) saved = 'all';
  apply(saved);
})();
</script>"""
    return page("Bargain Agent Demo Run Sheet", body, script, fragment)


def personal(who: str) -> str:
    p = PEOPLE[who]
    idx = ORDER.index(who)
    prev, nxt = ORDER[idx - 1], (ORDER[idx + 1] if idx + 1 < len(ORDER) else None)
    prev_last = [s for s in SEGMENTS if s[0] == prev][-1][4][-1][0]
    mine = [s for s in SEGMENTS if s[0] == who]
    start, end = spans()[who]
    length = clock(seconds(end) - seconds(start))
    words = sum(len(say.split()) for s in mine for say, _ in s[4])
    after = (f"讲完你的最后一句，<b>{escape(PEOPLE[nxt]['name'])}</b> 接着讲。" if nxt
             else "你是最后一个。讲完 “Thank you for listening…” 后全组一起进入提问。")
    body = f"""<header><span class="label">Bargain Agent 演示 · 第 {idx + 1} 位</span><h1>{escape(p['name'])} 的演示稿</h1>
<p class="lede">只有你的部分。演讲词用英文照读，右边是讲到这一句时屏幕上做什么。</p>
<div class="facts"><div><span class="label">开始</span><b class="mono">{start}</b></div><div><span class="label">时长</span><b class="mono">{length}</b></div><div><span class="label">字数</span><b class="mono">≈{words}</b></div><div><span class="label">角色</span><b>{escape(p['role'])}</b></div></div></header>
<section class="panel" aria-labelledby="cue-h"><h2 id="cue-h">什么时候开始</h2>
<p>听到 <span class="who" data-s="{prev}">{escape(PEOPLE[prev]['name'])}</span> 说完这一句，你就开始：</p>
<p class="cueline" data-s="{prev}">“{escape(prev_last)}”</p><p>{after}</p></section>
<section class="segments" aria-label="你的台词">{"".join(segment(s) for s in mine)}</section>
<div class="grid2"><section class="panel" aria-labelledby="re-h"><h2 id="re-h">怎么排练</h2><ol>
<li>解压演示包后，双击 <code>BargainAgent</code> 文件夹里的“演示模式.bat”，浏览器会打开演示页面（黑色窗口不要关）。</li>
{"".join(f"<li>{tip}</li>" for tip in REHEARSE[who])}
<li>对着计时器读两遍，控制在 {length} 左右。英文慢一点、清楚一点比快更重要。</li></ol></section>
<section class="panel" aria-labelledby="qa-h"><h2 id="qa-h">可能问到你的问题</h2>{qa(QA_FOR[who])}</section></div>
<section class="panel" aria-labelledby="order-h"><h2 id="order-h">全组顺序</h2>{bar()}{cast_table(who)}</section>"""
    return page(f"{p['short']} Demo Lines", body)


def main() -> int:
    (OUT / "run-sheet.html").write_text(run_sheet(), encoding="utf-8")
    team = OUT / "team"
    team.mkdir(exist_ok=True)
    for old in team.glob("*.html"):
        old.unlink()
    for n, who in enumerate(ORDER, 1):
        if who == "fw":
            continue
        (team / f"{n}-{PEOPLE[who]['short']}.html").write_text(personal(who), encoding="utf-8")
    print("wrote", OUT / "run-sheet.html", "and", ", ".join(sorted(p.name for p in team.glob("*.html"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
