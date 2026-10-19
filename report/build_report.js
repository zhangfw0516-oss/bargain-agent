// Builds the final project report: 159333_Final_Report_Bargain_Agent.docx
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType, Table, TableRow, TableCell,
  WidthType, ShadingType, BorderStyle, ImageRun, PageBreak, TableOfContents, Footer, PageNumber,
  LevelFormat, Header,
} = require("docx");

const FIG = path.join(__dirname, "fig");
const OUT = process.argv[2] || path.join(__dirname, "159333_Final_Report_Bargain_Agent.docx");

const FONT = "Calibri";
const MONO = "Consolas";
const CONTENT_W = 9026; // A4 width 11906 minus 2 x 1440 margins
const ACCENT = "1D5F8C";

// ---------- inline markup: **bold**, *italic*, `code` ----------
function runs(text, base = {}) {
  const out = [];
  const re = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(new TextRun({ text: text.slice(last, m.index), ...base }));
    const t = m[0];
    if (t.startsWith("**")) out.push(new TextRun({ text: t.slice(2, -2), bold: true, ...base }));
    else if (t.startsWith("`")) out.push(new TextRun({ text: t.slice(1, -1), font: MONO, size: 20, ...base }));
    else out.push(new TextRun({ text: t.slice(1, -1), italics: true, ...base }));
    last = m.index + t.length;
  }
  if (last < text.length) out.push(new TextRun({ text: text.slice(last), ...base }));
  return out;
}

const P = (text, opts = {}) => new Paragraph({ children: runs(text, opts.run || {}), spacing: { after: 140, line: 300 }, alignment: opts.align, ...opts.para });
const H1 = (text) => new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun(text)], pageBreakBefore: true });
const H2 = (text) => new Paragraph({ heading: HeadingLevel.HEADING_2, children: [new TextRun(text)] });
const H3 = (text) => new Paragraph({ heading: HeadingLevel.HEADING_3, children: [new TextRun(text)] });
const BUL = (text) => new Paragraph({ numbering: { reference: "bullets", level: 0 }, children: runs(text), spacing: { after: 80, line: 290 } });
const NUM = (text, ref = "steps") => new Paragraph({ numbering: { reference: ref, level: 0 }, children: runs(text), spacing: { after: 80, line: 290 } });
const BREAK = () => new Paragraph({ children: [new PageBreak()] });

let figNo = 0, tabNo = 0;
function pngSize(file) {
  const b = fs.readFileSync(file);
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) };
}
function FIGURE(file, caption, widthPx = 600) {
  const full = path.join(FIG, file);
  const { w, h } = pngSize(full);
  const height = Math.round(widthPx * h / w);
  figNo += 1;
  return [
    new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 120, after: 60 },
      children: [new ImageRun({ type: "png", data: fs.readFileSync(full), transformation: { width: widthPx, height },
        altText: { title: `Figure ${figNo}`, description: caption, name: file } })] }),
    new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 220 },
      children: [new TextRun({ text: `Figure ${figNo}. `, bold: true, size: 20 }), ...runs(caption, { size: 20, color: "444444" })] }),
  ];
}

const border = { style: BorderStyle.SINGLE, size: 4, color: "BFC7D2" };
const borders = { top: border, bottom: border, left: border, right: border };
function TABLE(caption, header, rows, widths) {
  const total = widths.reduce((a, b) => a + b, 0);
  const scale = CONTENT_W / total;
  const w = widths.map((x) => Math.round(x * scale));
  w[w.length - 1] += CONTENT_W - w.reduce((a, b) => a + b, 0);
  const cell = (text, i, head) => new TableCell({
    width: { size: w[i], type: WidthType.DXA }, borders,
    shading: head ? { fill: "E4EEF6", type: ShadingType.CLEAR, color: "auto" } : undefined,
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    children: [new Paragraph({ children: runs(text, head ? { bold: true, size: 20 } : { size: 20 }), spacing: { after: 0, line: 260 } })],
  });
  tabNo += 1;
  return [
    new Paragraph({ spacing: { before: 160, after: 80 }, keepNext: true,
      children: [new TextRun({ text: `Table ${tabNo}. `, bold: true, size: 20 }), ...runs(caption, { size: 20, color: "444444" })] }),
    new Table({ width: { size: CONTENT_W, type: WidthType.DXA }, columnWidths: w,
      rows: [new TableRow({ tableHeader: true, children: header.map((t, i) => cell(t, i, true)) }),
        ...rows.map((r) => new TableRow({ children: r.map((t, i) => cell(t, i, false)) }))] }),
    new Paragraph({ spacing: { after: 120 }, children: [] }),
  ];
}

function CODE(lines, caption) {
  const out = [];
  if (caption) out.push(new Paragraph({ spacing: { before: 120, after: 60 }, keepNext: true, children: runs(caption, { size: 20, color: "444444", italics: true }) }));
  lines.forEach((line, i) => out.push(new Paragraph({
    shading: { fill: "F2F4F7", type: ShadingType.CLEAR, color: "auto" },
    spacing: { after: 0, line: 250, before: i === 0 ? 40 : 0 }, keepNext: i < lines.length - 1,
    indent: { left: 200, right: 200 },
    children: [new TextRun({ text: line.length ? line.replace(/ /g, " ") : " ", font: MONO, size: 18 })],
  })));
  out.push(new Paragraph({ spacing: { after: 160 }, children: [] }));
  return out;
}

function BOX(lines) { // quoted text, e.g. the real alert email
  return lines.map((line, i) => new Paragraph({
    shading: { fill: "FBF7EF", type: ShadingType.CLEAR, color: "auto" },
    border: i === 0 ? { top: { style: BorderStyle.SINGLE, size: 6, color: "C98B4A", space: 4 } } :
      (i === lines.length - 1 ? { bottom: { style: BorderStyle.SINGLE, size: 6, color: "C98B4A", space: 4 } } : undefined),
    indent: { left: 200, right: 200 }, spacing: { after: 0, line: 260 },
    children: [new TextRun({ text: line.length ? line : " ", font: MONO, size: 17 })],
  })).concat([new Paragraph({ spacing: { after: 160 }, children: [] })]);
}

const PLACEHOLDER = (text) => new Paragraph({ spacing: { after: 140, line: 300 },
  shading: { fill: "FFF4D6", type: ShadingType.CLEAR, color: "auto" },
  children: [new TextRun({ text, italics: true, color: "7A5A00" })] });

// ---------------------------------------------------------------- content
const team = [
  ["Fengwei Zhang", "24009491", "Project Lead"],
  ["Gang Zhao", "24009365", "Data Engineer"],
  ["Tianshuo Gao", "24009350", "Interface Developer"],
  ["Xiao Zhang", "24009323", "Agent Developer and Test Engineer"],
  ["Wenhan Zhang", "24009400", "Agent Developer and Test Engineer"],
];

const titlePage = [
  new Paragraph({ spacing: { before: 2200, after: 200 }, children: [new TextRun({ text: "159.333 Computer Science Project", size: 26, color: ACCENT, bold: true })] }),
  new Paragraph({ spacing: { after: 200 }, children: [new TextRun({ text: "Bargain Agent", size: 64, bold: true, color: "14213D" })] }),
  new Paragraph({ spacing: { after: 600 }, children: [new TextRun({ text: "An agentic AI system that finds products and monitors bargain prices across New Zealand retailers", size: 30, color: "46546A" })] }),
  new Paragraph({ spacing: { after: 160 }, children: [new TextRun({ text: "Final Project Report", size: 28, bold: true })] }),
  new Paragraph({ spacing: { after: 600 }, children: [new TextRun({ text: "Massey University · Semester 2, 2026 · October 2026", size: 24, color: "46546A" })] }),
  ...TABLE("Project team", ["Name", "Student ID", "Role"], team, [3, 2, 4]),
  new Paragraph({ spacing: { before: 200 }, children: [new TextRun({ text: "Supervisor: Dr Surangika Ranathunga", size: 22, color: "46546A" })] }),
];

const contents = [
  new Paragraph({ pageBreakBefore: true, spacing: { after: 220 }, children: [new TextRun({ text: "Contents", bold: true, size: 34, color: "14213D" })] }),
  new TableOfContents("Contents", { hyperlink: true, headingStyleRange: "1-2" }),
];

const intro = [
  H1("1. Introduction"),
  H2("1.1 Purpose of the project"),
  P("Online prices in New Zealand change often and differ between shops. A person who wants a pair of headphones or an air fryer usually checks PB Tech, Noel Leeming, JB Hi-Fi or Mighty Ape by hand, again and again, and still misses the sale. Existing price alert tools usually require the user to find the exact product page first and fill in a form."),
  P("**Bargain Agent** lets a user describe what they want in their own words, in English or Chinese, for example *\"noise cancelling headphones under NZD 400\"*. A pipeline of AI agents turns the description into real products found in New Zealand shops, compares the same product across merchants, keeps watching the price, and sends an email when the price becomes a real deal. The project brings together three ideas from our proposal: autonomous shopping agents, natural-language interaction through a large language model (LLM), and scheduled monitoring with notifications."),
  H2("1.2 Who will use it"),
  P("The system is a personal tool for one user on their own Windows computer: a shopper in New Zealand who wants to buy a specific kind of product at a good price but does not want to check shops every day. It runs locally and is used through a web page in the browser. A command-line interface is also available for technical users and for scheduled checks."),
  H2("1.3 Goals"),
  P("The proposal set five goals. All five were delivered, and the way they were delivered changed during the project, as Section 3 explains."),
  BUL("Autonomous, scheduled price monitoring across several New Zealand retailers."),
  BUL("Natural-language interaction, so that monitoring tasks are created conversationally."),
  BUL("A reliable notification system that alerts the user to price drops."),
  BUL("An evaluation of price extraction, instruction understanding and alert triggering."),
  BUL("A working Python prototype with clear documentation."),
  H2("1.4 Team and module ownership"),
  P("The system was split into four modules along the flow of data, and each member owned one part. The project lead owned the architecture, the integration code that connects the modules, the evaluation summary and the delivery. Table 2 lists the owners; the complete list of files per member is kept in `docs/TEAM_RESPONSIBILITIES.md` in the source code."),
  ...TABLE("Module ownership", ["Member", "Owns"], [
    ["Fengwei Zhang", "Architecture and key decisions; integration code (`pipeline.py`, `llm.py`, `config.py`); evaluation summary; packaging and delivery; documentation"],
    ["Gang Zhao", "Data layer: search providers and page readers (`sources/`), price observation (`pricing.py`), SQLite storage (`storage.py`), real price collection"],
    ["Tianshuo Gao", "Interface: web UI (`static/index.html`, `webapp.py`), command-line task management (`cli.py`), email notifications (`notifier.py`)"],
    ["Xiao Zhang", "Find agents (Understand, Rewrite, Select, Describe) and same-product matching (`matching.py`, offer verifier)"],
    ["Wenhan Zhang", "Monitoring loop and deal decisions (`scheduler.py`), instruction parsing and price extraction agents, output validation (`schemas.py`)"],
  ], [2, 7]),
  H2("1.5 Use of AI tools"),
  P("AI-assisted programming tools were used during the development of this project."),
];

const background = [
  H1("2. Background"),
  P("Shopping agents are an old idea. ShopBot learned how to query online stores and extract prices, and compared offers across vendors automatically (Doorenbos et al., 1997). Maes et al. (1999) described how software agents could take over parts of buying and selling, such as finding products, comparing merchants and negotiating. More recently, Jorge et al. (2020) showed that web scraping can support price monitoring in a real retail sector, but scraping depends on the structure of each site."),
  P("Large language models make a new kind of agent possible. ReAct showed that a model can interleave reasoning with actions such as searching (Yao et al., 2023), and Wang et al. (2024) survey the growing field of LLM-based autonomous agents, which plan, use tools and act on behalf of a user. The same models can also produce fluent text that is not true, a problem known as hallucination (Ji et al., 2023). For a shopping agent, an invented price or product would be worse than no answer at all. This shaped the most important design rule of our system: **agents choose, code decides** (Section 3.4)."),
  P("Our project combines these threads: the comparison-shopping idea of ShopBot, LLM agents for understanding and choosing, and strict code-level checks so that every price shown and every email sent can be traced to real data."),
];

const decisions = [
  H1("3. Project evolution and key decisions"),
  P("The final system is quite different from the proposal. This section explains what changed and why, because these decisions explain most of the design."),
  H2("3.1 The original approach: scraping each retailer"),
  P("The proposal planned a web scraping module built with Requests and BeautifulSoup for static pages and Selenium for pages rendered by JavaScript (Jorge et al., 2020). In the first weeks we built retailer adapters for PB Tech and JB Hi-Fi, and verified real prices from both. A monitoring task, created from a sentence by an LLM parser, could watch a product page and send an email when the price fell below a threshold."),
  H2("3.2 What went wrong"),
  P("When we tried to cover more shops, the approach broke down:"),
  BUL("Mighty Ape returned HTTP 403 for the whole site, served by an anti-bot service (DataDome). Noel Leeming and other detail pages were also blocked or rendered only in the browser."),
  BUL("Every retailer needed its own parser, and a parser stopped working whenever the shop changed its page layout."),
  BUL("Finding products from a description required searching every shop separately, which multiplied both problems."),
  P("We decided not to work around anti-bot protection. Bypassing it would be fragile and would not respect the shops' choices."),
  H2("3.3 Decision: read Google Shopping through search APIs"),
  P("We decided to read Google Shopping New Zealand through commercial search APIs instead of scraping shops, because one search returns structured listings, with title, merchant, price and a product identifier, for every merchant at once. We use **Serper** as the main provider and **SerpApi** as a backup and for product details (Serper, n.d.; SerpApi, n.d.). Both return the same Google product identifiers, so a monitor can later find the same listing again. We only read a shop's own page when the user pastes a specific link, and then through a chain of three page-reading services (Scrape.do, Firecrawl and Tavily), so one blocked service does not stop the feature (Firecrawl, n.d.)."),
  P("We considered a general web search API (Brave Search) and an AI search API (Tavily), which we had integrated earlier for finding candidate links. They return web pages, not prices, so every result still had to be scraped. A shopping API removed that step."),
  H2("3.4 Decision: a pipeline of single-purpose AI agents"),
  P("We decided to build the \"find\" feature as a pipeline of small agents, each with one job, instead of one large prompt. Small agents are easier to test, validate and replace, and if one fails only that step falls back to plain code. We built the pipeline with LangGraph, because our flow is a graph with a condition (go back and search again if nothing suitable was found) and LangGraph expresses that directly while keeping a shared state between steps (LangChain, n.d.)."),
  P("Because of the hallucination risk discussed in Section 2, we set one rule for every module: **agents choose, code decides**. An agent may only pick listings by identifiers that our server assigned, so it cannot invent a product or a price. Every model output is validated. Budgets, deal thresholds, duplicate checks and whether an email may be sent are decided by plain code that we can test."),
  H2("3.5 Other decisions"),
  ...TABLE("Further design decisions and the reasons for them", ["Decision", "Reason"], [
    ["Python 3.13 with few dependencies", "The team knew Python; a small dependency list (Requests, LangGraph, python-dotenv, schedule) keeps installation and packaging simple."],
    ["A local web app on 127.0.0.1, not a cloud service", "It is a personal tool for one user; API keys and data stay on the user's computer and no server is needed."],
    ["SQLite for storage", "No database server to install; price history fits easily; schema versions are handled with simple migrations (Python Software Foundation, n.d.-a)."],
    ["DeepSeek through an OpenAI-compatible API", "Supports JSON output and is low cost; because the client is compatible, models can be changed in the settings file, even per agent (DeepSeek, n.d.)."],
    ["Only trusted merchants trigger alerts", "Unknown marketplace and overseas sellers are shown for comparison, but an email should only go out for a deal that is safe to buy."],
    ["Email only, no SMS", "The proposal mentioned SMS. Email was enough for our users, and SMS needs a paid service, so we left it as future work."],
    ["A Windows package built with PyInstaller", "Teammates and users can run the system without installing Python (PyInstaller Development Team, n.d.)."],
  ], [3, 6]),
  P("All decisions, with their dates and reasons, are recorded in `project_progress/DECISIONS.md` in the source code."),
];

const design = [
  H1("4. System design"),
  H2("4.1 Architecture"),
  P("Figure 1 shows the architecture. The system has four layers, each owned by one team member: the interface the user sees, the AI agents, the monitoring loop, and the data layer that reads prices and stores them. The layers do not depend on each other's internal details. They meet through a small integration layer: the LangGraph pipeline, a single entry point for all model calls, and configuration. The interfaces between layers were written down first, in `docs/INTERFACES.md`, so that team members could work in parallel."),
  ...FIGURE("architecture.png", "Architecture: four layers connected by an integration layer, and the external services the system uses."),
  H2("4.2 Main flows"),
  P("The system has two main flows (Figure 2). In **Find**, a description goes through the Understand, Rewrite, Select and Describe agents, with a plain-code Search step that calls the shopping API. If Select finds nothing suitable, the graph goes back to Rewrite for new search terms, within a fixed budget. In **Monitor**, the chosen product is matched at other merchants, stored as a task with up to five merchants, and checked on a schedule; a deal at a trusted merchant sends one email."),
  ...FIGURE("pipeline.png", "The Find pipeline (top) and the monitoring flow (bottom). Purple boxes are AI agents; all decisions about deals and emails are made in code."),
  H2("4.3 Technologies"),
  ...TABLE("Technologies used", ["Area", "Technology", "Used for"], [
    ["Language", "Python 3.13", "All application code and tests"],
    ["Agent orchestration", "LangGraph 1.2", "The Find pipeline as a state graph with a conditional retry edge"],
    ["Language model", "DeepSeek (deepseek-flash), OpenAI-compatible API", "Eight agents; JSON output mode"],
    ["Shopping data", "Serper (primary), SerpApi (backup, product details)", "Google Shopping New Zealand listings and prices"],
    ["Page reading", "Scrape.do, Firecrawl, Tavily", "Prices from a product page the user pastes"],
    ["Storage", "SQLite", "Tasks, merchants, price history, sent alerts, API usage"],
    ["Web interface", "Python http.server, HTML, CSS, JavaScript", "Local web app; price chart drawn as SVG"],
    ["Email", "smtplib with Gmail (app password)", "Alert emails (Python Software Foundation, n.d.-b)"],
    ["Testing", "unittest, recorded API responses, fake model", "362 offline tests and an evaluation runner"],
    ["Packaging", "PyInstaller", "Windows program, no Python needed"],
  ], [2, 3, 4]),
  P("The application has about 6,600 lines of Python in 32 files, a 1,000-line web page, eight agent prompts, and about 4,500 lines of tests."),
];

const implementation = [
  H1("5. Implementation"),
  H2("5.1 The eight AI agents"),
  P("Table 5 lists the agents. Each agent is a small Python module with a prompt file, a validation function and a rule-based fallback."),
  ...TABLE("The eight AI agents", ["Agent", "Job", "Used in"], [
    ["Understand", "Turns a description into a structured need: category, budget, currency, condition, brand", "Find"],
    ["Rewrite", "Writes up to three search queries", "Find"],
    ["Select", "Picks up to eight products from at most 30 listings, by listing ID only", "Find"],
    ["Describe", "Writes a short summary of each pick, in English with a Chinese version", "Find"],
    ["Offer verifier", "Checks whether another merchant sells the same product; it can only veto", "Compare, monitor"],
    ["Task parser", "Turns a one-sentence instruction into a monitoring task, or asks for missing details", "Add by sentence"],
    ["Price extractor", "Reads the price from the text of a shop page", "Pasted links"],
    ["Deal summary", "Optional short summary at the top of an alert email (off by default)", "Alert email"],
  ], [2, 5, 2]),
  H2("5.2 Connecting the agents with LangGraph"),
  P("The Find pipeline is built in `build_graph()` in `pipeline.py`. Each step is a node; the edges fix the order, and a conditional edge after Select decides whether to retry. The decision function checks that Select ran successfully but chose nothing, and that there is still rewrite budget, search budget and time left (at most three queries and 120 seconds per search). Each node is wrapped so that it reports its name to the web page, which shows a progress bar. Several search queries run in parallel threads."),
  ...CODE([
    'graph = StateGraph(FindState)',
    'graph.add_node("understand", staged("understand", understand_node))',
    'graph.add_node("rewrite", staged("rewrite", rewrite_node))',
    'graph.add_node("search", staged("search", search_node))',
    'graph.add_node("select", staged("select", select_node))',
    'graph.add_node("describe", staged("describe", describe_node))',
    'graph.add_edge(START, "understand")',
    'graph.add_edge("understand", "rewrite")',
    'graph.add_edge("rewrite", "search")',
    'graph.add_edge("search", "select")',
    'graph.add_conditional_edges("select", after_select,',
    '                            {"rewrite": "rewrite", "describe": "describe"})',
    'graph.add_edge("describe", END)',
  ], "Code excerpt 1. pipeline.py, build_graph(): the Find pipeline as a LangGraph state graph."),
  P("A user can also refine a result in conversation, for example *\"only trusted NZ shops\"* or *\"只要 Sony 的\"*. The earlier listings are kept, at most two new searches are made, and the agents choose again."),
  H2("5.3 One validated entry point for the model"),
  P("All eight agents call the model through one function, `call_json()` in `llm.py`. It asks for JSON output, passes the parsed answer to the agent's own validation function, and retries once with the error fed back if the answer is invalid or was cut off. If it still fails, it raises an error and the agent uses its rule-based fallback, so a search still returns results and the page reports that some agents used fallbacks. Each agent can be given a different model in the settings file."),
  ...CODE([
    'def call_json(prompt_name: str, payload: Any, *,',
    '              validate=None, agent=None, retries=1,',
    '              max_tokens=MAX_RESPONSE_TOKENS) -> Any:',
    '    """Run one agent prompt and return its validated JSON output."""',
  ], "Code excerpt 2. llm.py, call_json() (signature shortened)."),
  P("A real problem shaped this function. The model we use reasons before it answers, and the reasoning counted against the response limit, so some answers were cut off and the Select agent fell back to plain ranking. Detecting a cut-off answer and raising the limit fixed it."),
  H2("5.4 Data providers and price verification"),
  P("All providers implement the same small interfaces in `sources/base.py` (shopping search, offer source, page extractor), so a provider can be replaced without changing the rest. A fallback wrapper uses SerpApi when Serper fails. Every provider call passes through a quota guard that counts calls per day in SQLite and stops at a configured limit."),
  P("Prices read from shop pages need extra care. When a page contains exactly one structured product offer (JSON-LD, as defined by Schema.org), we use that price directly (Schema.org, n.d.). Otherwise the price extractor agent reads the page text, and its answer is only accepted if the same price appears on the page. The prompt excludes prices before GST, instalment amounts and recommended retail prices, and treats a bare \"$\" on a .nz site as NZD."),
  H2("5.5 Same-product matching"),
  P("When the user chooses a product, `matching.py` looks for the same product at other merchants. Rules come first: brand and model words must match, the item must be new unless the user asked otherwise, and the price must be within 50% to 150% of the chosen product, which removes accessories and bundles. The offer verifier agent then checks the remaining candidates and can only reject them. One listing per merchant is kept, at most five merchants are watched, and trusted merchants are ticked by default (Figure 4)."),
  H2("5.6 Monitoring and alerts"),
  P("Each scheduled check runs one shared Google Shopping search for the whole task, however many merchants it watches, and reads each merchant's current price by product identifier. If a merchant is missing from the results, at most two retry searches and two detail lookups are made. The scheduler then excludes offers the verifier marked as different, takes the cheapest offer from a trusted merchant, and decides in code whether it is a deal: the price is at or below the target, or it has dropped by a chosen percentage since the last alert."),
  P("A send permit in the database ensures that one price drop sends one email, even if two checks run at the same time, and task locks prevent duplicate checks. From the stored history the system also computes the 30-day low and average from the cheapest usable price of each check, and can compare delivered prices (item plus shipping) when shipping is known. Monitoring can run continuously in the web app, or once per run through a Windows scheduled task (`check-due`)."),
  H2("5.7 Web interface and email"),
  P("The interface is a local web page served by Python's standard HTTP server (Figures 3 to 5). A search runs as a background job, and the page polls its progress so the user sees which agent is working. Results are cards with the price, the merchant, whether it is trusted, and the summary in English and Chinese. The detail view shows a price chart drawn as SVG, the latest price at each merchant, shipping, and the alert settings."),
  ...FIGURE("find.png", "A real search for \"noise cancelling headphones under NZD 400\": the search terms written by the Rewrite agent, and product cards with bilingual summaries."),
  ...FIGURE("preview.png", "Compare and monitor: the same product at other merchants, checked by the offer verifier (\"AI: same product\"). Trusted merchants are ticked by default."),
  ...FIGURE("detail.png", "A real monitor for the Fitbit Charge 6 at two merchants, with its price trend from 6 to 14 October 2026 and the 30-day comparison. The owner's email address is hidden."),
  P("The alert email is built in `notifier.py` and sent through Gmail's SMTP server with an app password. Below is the text of a real alert sent by the system on 13 October 2026, after a test target of NZD 280 was set on the monitor in Figure 5. It also shows a useful property of the 30-day line: the price met the target, but the email tells the user it is not below the recent average."),
  ...BOX([
    "Subject: [Bargain Agent] 降价提醒：Fitbit Charge 6 Fitness Tracker — NZD 275.00 @ Harvey Norman New Zealand",
    "",
    "降价提醒 / Price alert: Harvey Norman New Zealand 现价 NZD 275.00（目标 NZD 280.00）",
    "A price match was found for your monitored item.",
    "Product: Fitbit Charge 6 Fitness Tracker - Silver Aluminum Case with Porcelain Band ...",
    "Current price: NZD 275.00",
    "Your threshold: NZD 280.00 (lte)",
    "Price history: Not below the 30-day average (low NZD 198.00, average NZD 255.75, 4 checks).",
    "Retailer: Harvey Norman New Zealand",
    "In stock: True",
    "Shipping cost: unknown (price does not include verified shipping).",
    "Other monitored sources (1):",
    "  - PB Tech: NZD 289.00",
  ]),
  H2("5.8 Security and cost control"),
  BUL("The server listens only on 127.0.0.1. Every request that changes data needs a CSRF token, and the Origin and Host headers are checked (OWASP Foundation, n.d.). Request bodies are limited to 64 KB."),
  BUL("API keys are kept in a local `.env` file that git ignores. The settings page can set the recipient and sending mailbox, but never shows saved values."),
  BUL("Each provider has a daily call limit (by default 200 for Serper and 20 for SerpApi). One check uses one shared search, a task watches at most five merchants, and a search uses at most three queries."),
  BUL("Checking a task or starting continuous monitoring asks for confirmation, because it can send a real email."),
  H2("5.9 Packaging and demo mode"),
  P("`build_package.py` builds a Windows program with PyInstaller. A user unzips it and double-clicks `BargainAgent.exe`, which starts the server and opens the browser. The settings file, database and logs live next to the program. A source package installs its own Python environment on first run. For presentations we added a demo mode with a separate database, in which earlier price points are simulated around real current prices and the page shows a clear \"Demo data\" banner; search and price checks stay live."),
];

const evaluation = [
  H1("6. Testing and evaluation"),
  H2("6.1 Automated tests"),
  P("The project has 362 automated tests that run offline in about two minutes. They cover every agent, the providers (using recorded real responses), matching, the scheduler, database migrations, the web API and the command line. Model answers are replaced by a fake client, and a guard test fails if any test tries to reach the real network, so the tests are free and repeatable. We followed a simple rule throughout: a change was complete only when all tests passed (Sommerville, 2016)."),
  H2("6.2 Evaluation plan"),
  P("The proposal defined three accuracy measures: price extraction, instruction parsing and notification triggering. We measured all three with real data on 5 October 2026, using the DeepSeek model. Figure 6 summarises the results, and `docs/EVALUATION.md` gives the details."),
  ...FIGURE("evaluation.png", "Evaluation results on real data (5 October 2026)."),
  H2("6.3 Real price accuracy"),
  P("For 17 common New Zealand products we took the first new, trusted-merchant listing from Google Shopping, opened the merchant's own page, read its price, and compared it with Google's price. Two queries had no suitable trusted listing. The merchants were PB Tech, Noel Leeming, Heathcotes, Mighty Ape and Harvey Norman."),
  ...TABLE("Real price accuracy", ["Run", "Page reading", "Compared", "Exact match"], [
    ["First run", "Firecrawl JSON extraction", "15", "9 (60%)"],
    ["Re-read after fixes", "JSON-LD first, else AI on page text", "14", "14 (100%)"],
  ], [2, 4, 2, 2]),
  P("We analysed every miss in the first run. The causes were specific and each led to a fix:"),
  ...TABLE("Causes of wrong prices and their fixes", ["Cause", "Example", "Fix"], [
    ["Superscript cents read as one number", "\"$ 448 00\" read as 44800", "Use the page's JSON-LD price; an AI price must appear on the page"],
    ["Instalment taken as the price", "Afterpay amount, a quarter of the price", "Prompts exclude instalment amounts"],
    ["Recommended retail price taken", "RRP instead of the sale price", "Prompts exclude was/RRP prices"],
    ["Price before GST", "\"$172.17 +GST / $198.00 inc GST\"", "Prompts ask for the tax-inclusive price"],
  ], [3, 3, 4]),
  H2("6.4 Agents with the real model"),
  ...TABLE("Real model evaluation", ["Track", "Cases", "Result"], [
    ["Price extraction from page text", "13", "13/13 exact; false-price rate 0"],
    ["Instruction parsing", "16", "15/16 fully correct; 98.1% of fields"],
    ["Offer price parsing (code)", "26", "100%"],
    ["Alert triggering (real scheduler)", "10 scenarios, 19 steps", "Precision 1.0, recall 1.0, no unexpected email"],
  ], [3, 2, 4]),
  P("The price extractor never reported a false price, including on a page that tried to inject instructions. The one instruction that was not fully parsed contained only a link and no product name; the model asked for clarification instead of guessing, which is the behaviour we want."),
  H2("6.5 End to end"),
  P("We also ran the whole system with real services: a real search for a Fitbit Charge 6, a monitor on two merchants (PB Tech and Harvey Norman, matched by the verifier), a real check that sent one email, and a second check that correctly sent no duplicate. The alert in Section 5.7 is a later real email from the same monitor."),
  H2("6.6 Limits of the evaluation"),
  P("The real price sample was 17 products, slightly below the 20 we planned, because each product costs paid API calls. We have not yet run the one-week continuous monitoring test from the proposal. Prices on shop pages can also change between the moment Google records them and the moment we read the page, so a small number of mismatches in a larger sample would not necessarily be errors."),
];

const problems = [
  H1("7. Problems and how we solved them"),
  ...TABLE("Main problems during the project", ["Problem", "What we did"], [
    ["Retailers blocked our scraper (HTTP 403, anti-bot services)", "Changed to shopping search APIs instead of bypassing protection (Section 3.3)"],
    ["Wrong prices from shop pages", "Analysed each miss; JSON-LD first, on-page check for AI prices, stricter prompts (Section 6.3)"],
    ["Model answers cut off, agent fell back", "Detected cut-off answers and raised the response limit for reasoning models"],
    ["Google product links from the API showed \"no details\" when opened directly", "Links now open a normal search for the product and merchant"],
    ["The first real alert email was not noticed", "Made the subject distinctive: \"[Bargain Agent] 降价提醒 … @ merchant\""],
    ["Saved search results appeared in the wrong mode (demo vs real)", "Results are saved per database, identified by a random instance id"],
    ["Teammates could not create monitors in the demo package", "Demo mode works without an email address; errors also appear as pop-up messages"],
    ["Installation failed in very long folder paths on Windows", "The start script explains the problem and suggests a short path"],
  ], [4, 5]),
];

const manual = [
  H1("8. User manual"),
  H2("8.1 Running the Windows program"),
  NUM("Unzip `BargainAgent-windows.zip` to a normal folder such as the Desktop.", "steps"),
  NUM("Double-click `BargainAgent.exe`. A console window opens and the browser shows http://127.0.0.1:8765/. Keep the window open while using the app.", "steps"),
  NUM("On the Settings page, enter the email address that should receive alerts and the sending mailbox (for Gmail, an app password).", "steps"),
  H2("8.2 Running from source"),
  P("Install Python 3.10 or newer, unzip the source package, and double-click `start.bat`. On first run it creates a Python environment and installs the requirements. Without keys it opens `.env` so that the language model and search keys can be entered."),
  H2("8.3 Finding a product and creating a monitor"),
  NUM("On the Find page, describe the product in English or Chinese and press *Find with AI agents*.", "find"),
  NUM("Optionally refine the results, for example *Only trusted NZ shops*.", "find"),
  NUM("Press *Compare & monitor* on a card, tick the merchants to watch, set the target price, and press *Create monitor*.", "find"),
  NUM("On the Monitors page, use *Check now* for an immediate check, or *Start monitoring* to check on schedule. *Details* shows the price chart and merchants.", "find"),
  H2("8.4 Checking without keeping the window open"),
  P("Register the scheduled check with Windows Task Scheduler, for example every 30 minutes:"),
  ...CODE(['schtasks /Create /SC MINUTE /MO 30 /TN "BargainAgent check-due" /TR "<folder>\\BargainAgent.exe --check-due"']),
  P("Each task is still checked only at its own frequency. The computer must be on at check time."),
];

const conclusion = [
  H1("9. Conclusion"),
  P("Bargain Agent meets the goals of our proposal. A user can describe a product in their own words, see real products from New Zealand shops chosen by AI agents, compare the same product across merchants, and receive an email when the price meets their target. The most important lesson of the project was a decision rather than a feature: when scraping retailers failed, we changed our data source instead of fighting anti-bot protection, and that made the rest of the system possible."),
  P("The second lesson was about trust. By letting agents choose only from real listings and keeping every decision about money and emails in code, we could test the system and measure it: prices matched the shops' own pages in 14 of 14 cases after our fixes, the price agent never reported a false price, and alerts were triggered with precision and recall of 1.0. The main limitations are the size of the real sample, the dependence on what Google Shopping New Zealand lists, and the need for the computer to be on for monitoring."),
];

const future = [
  H1("10. Future work"),
  BUL("Run the one-week continuous monitoring test and extend the real price sample to 20 or more products."),
  BUL("A cloud version, so that monitoring continues when the user's computer is off, with user accounts and shared searches for popular products."),
  BUL("More alert channels: SMS (from the proposal), Telegram or desktop notifications."),
  BUL("Let the user edit the list of trusted merchants in the interface."),
  BUL("Support other countries by changing the Google Shopping locale and the trusted merchant list."),
  BUL("Publish the code to a shared repository with continuous integration, so every change runs the tests automatically."),
];

const learning = [
  H1("11. Personal learning"),
  H3("Fengwei Zhang"),
  P("The most important thing I learned as project lead was to test a design against reality early. Our proposal depended on scraping retailer websites, and only when the big shops blocked us did we see how fragile that plan was. Changing to search APIs late in the semester cost time, and next time I would check the data source in the first week. I also learned how much coordination depends on clear interfaces: once the boundaries between modules were written down, five people could work at the same time. Finally, I learned that \"the tests pass\" is not the same as \"the system is correct\". Our real price accuracy was only 60% at first, and it reached 100% only after I went through every wrong price and found its cause. Working with AI-assisted programming tools taught me that the design decisions and the checking still have to be done by us."),
  H3("Gang Zhao"),
  P("Working on the data part of the project taught me that collecting prices from the web is harder than it looks. I had expected scraping to be a technical problem, but anti-bot services such as DataDome made it a question of what is reliable and acceptable, which is why we moved to search APIs. I also learned that a price on a page is not always the price: superscript cents, prices before GST and Afterpay instalments all produced wrong numbers until they were handled. Structured data (JSON-LD) turned out to be the most reliable source. On the storage side, I learned how SQLite can keep a simple price history and how a 30-day low and average can be computed from it. I also became more aware of cost, because every API call counts against a daily limit."),
  H3("Tianshuo Gao"),
  P("Through the interface and notification part I learned that a usable tool needs more than correct results. A search takes up to a minute, so the page had to show progress instead of appearing frozen, which taught me how a background job and polling work together. I also learned about security for a local web app: why the server only listens on the user's own computer and why every request that changes data needs a CSRF token. On the email side, I learned how SMTP works with a Gmail app password. One lesson came from a real test: the first alert email arrived but was not noticed, so the subject was changed to make it stand out. It showed me that looking at the system from the user's side matters as much as making it work."),
  H3("Xiao Zhang"),
  P("Working on the agents taught me how to use a large language model inside a real system rather than as a chat tool. Splitting the Find task into small agents, each with one job, made every step easier to test and to replace, and LangGraph showed me how such steps can be connected as a graph with a retry. The biggest lesson was that a model can produce answers that sound right but are not true. Our solution was to let the Select agent choose only from listing IDs the server had seen, to validate every output, and to fall back to simple rules when the model fails. I also learned how much the wording of a prompt matters, including examples of requests the agent should refuse. Same-product matching showed me that rules and AI work best together."),
  H3("Wenhan Zhang"),
  P("Working on monitoring and testing taught me that a scheduled background task needs careful rules. A check must not run twice at the same time, an error must not leave a task stuck, and one price drop must send only one email, even if two checks happen together. The send permit in the database was a simple idea that solved this. I also learned the value of offline tests: by replacing the language model with a fake client, using recorded API responses and blocking real network calls, our tests became free and repeatable. Evaluating the alerts showed me what precision and recall mean in practice: precision tells you that no email was sent by mistake, and recall tells you that no real deal was missed. Measuring both gave us confidence that the alerts behave correctly."),
];

const refs = [
  "Doorenbos, R. B., Etzioni, O., & Weld, D. S. (1997). A scalable comparison-shopping agent for the World-Wide Web. In *Proceedings of the First International Conference on Autonomous Agents* (pp. 39–48). ACM. https://doi.org/10.1145/267658.267666",
  "DeepSeek. (n.d.). *DeepSeek API documentation*. Retrieved October 18, 2026, from https://api-docs.deepseek.com/",
  "Firecrawl. (n.d.). *Firecrawl documentation*. Retrieved October 18, 2026, from https://docs.firecrawl.dev/",
  "Ji, Z., Lee, N., Frieske, R., Yu, T., Su, D., Xu, Y., Ishii, E., Bang, Y. J., Madotto, A., & Fung, P. (2023). Survey of hallucination in natural language generation. *ACM Computing Surveys, 55*(12), 1–38. https://doi.org/10.1145/3571730",
  "Jorge, O., Pons, A., Rius, J., Vintró, C., Mateo, J., & Vilaplana, J. (2020). Increasing online shop revenues with web scraping: A case study for the wine sector. *British Food Journal, 122*(11), 3383–3401. https://doi.org/10.1108/BFJ-07-2019-0522",
  "LangChain. (n.d.). *LangGraph documentation*. Retrieved October 18, 2026, from https://langchain-ai.github.io/langgraph/",
  "Maes, P., Guttman, R. H., & Moukas, A. G. (1999). Agents that buy and sell. *Communications of the ACM, 42*(3), 81–91. https://doi.org/10.1145/295685.295716",
  "OWASP Foundation. (n.d.). *Cross-site request forgery prevention cheat sheet*. Retrieved October 18, 2026, from https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html",
  "PyInstaller Development Team. (n.d.). *PyInstaller manual*. Retrieved October 18, 2026, from https://pyinstaller.org/en/stable/",
  "Python Software Foundation. (n.d.-a). *sqlite3: DB-API 2.0 interface for SQLite databases*. Retrieved October 18, 2026, from https://docs.python.org/3/library/sqlite3.html",
  "Python Software Foundation. (n.d.-b). *smtplib: SMTP protocol client*. Retrieved October 18, 2026, from https://docs.python.org/3/library/smtplib.html",
  "Schema.org. (n.d.). *Offer*. Retrieved October 18, 2026, from https://schema.org/Offer",
  "SerpApi. (n.d.). *Google Shopping API*. Retrieved October 18, 2026, from https://serpapi.com/google-shopping-api",
  "Serper. (n.d.). *Serper: The world's fastest and cheapest Google Search API*. Retrieved October 18, 2026, from https://serper.dev/",
  "Sommerville, I. (2016). *Software engineering* (10th ed.). Pearson.",
  "Wang, L., Ma, C., Feng, X., Zhang, Z., Yang, H., Zhang, J., Chen, Z., Tang, J., Chen, X., Lin, Y., Zhao, W. X., Wei, Z., & Wen, J.-R. (2024). A survey on large language model based autonomous agents. *Frontiers of Computer Science, 18*(6), Article 186345. https://doi.org/10.1007/s11704-024-40231-1",
  "Yao, S., Zhao, J., Yu, D., Du, N., Shafran, I., Narasimhan, K., & Cao, Y. (2023). ReAct: Synergizing reasoning and acting in language models. In *Proceedings of the International Conference on Learning Representations (ICLR 2023)*. https://arxiv.org/abs/2210.03629",
];
const references = [
  H1("References"),
  ...refs.map((r) => new Paragraph({ children: runs(r), spacing: { after: 140, line: 280 }, indent: { left: 720, hanging: 720 } })),
  H2("Source code"),
  P("Software repository (branch `final`): **https://github.com/zhangfw0516-oss/bargain-agent/tree/final**"),
  P("The repository contains the complete source code and its development history, from the first retailer adapters to the final system. `README.md` explains how to run it, `docs/` contains the architecture, interfaces and evaluation details, and `.env.example` lists the settings. API keys are not stored in the repository; each user provides their own in a local `.env` file."),
];

// ---------------------------------------------------------------- document
const doc = new Document({
  creator: "Bargain Agent team",
  title: "Bargain Agent: Final Project Report",
  styles: {
    default: { document: { run: { font: FONT, size: 22 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 34, bold: true, font: FONT, color: "14213D" }, paragraph: { spacing: { before: 120, after: 220 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 27, bold: true, font: FONT, color: ACCENT }, paragraph: { spacing: { before: 280, after: 120 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 24, bold: true, font: FONT, color: "14213D" }, paragraph: { spacing: { before: 200, after: 80 }, outlineLevel: 2 } },
    ],
  },
  numbering: { config: [
    { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 540, hanging: 300 } } } }] },
    ...["steps", "find"].map((ref) => ({ reference: ref, levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 540, hanging: 360 } } } }] })),
  ] },
  sections: [
    { properties: { page: { margin: { top: 1440, bottom: 1440, left: 1440, right: 1440 } } }, children: titlePage },
    {
      properties: { page: { margin: { top: 1440, bottom: 1440, left: 1440, right: 1440 }, pageNumbers: { start: 1 } } },
      headers: { default: new Header({ children: [new Paragraph({ alignment: AlignmentType.RIGHT,
        children: [new TextRun({ text: "Bargain Agent · Final Project Report", size: 18, color: "8A93A5" })] })] }) },
      footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER,
        children: [new TextRun({ children: [PageNumber.CURRENT], size: 18, color: "8A93A5" })] })] }) },
      children: [...contents, ...intro, ...background, ...decisions, ...design, ...implementation, ...evaluation,
        ...problems, ...manual, ...conclusion, ...future, ...learning, ...references],
    },
  ],
});

Packer.toBuffer(doc).then((buf) => { fs.writeFileSync(OUT, buf); console.log("written", OUT, `${figNo} figures, ${tabNo} tables`); });
