# Project Proposal: An Agentic AI System to Monitor Bargain Deals of Retailers

## 1. Team Information
- **Supervisor**: Dr Surangika Ranathunga
- **Course**: 159.333 Computer Science Project, Semester 2 2026

### Team Members
| Role | Student ID | Name | Email |
| :--- | :--- | :--- | :--- |
| **Member 1 (Project Lead)** | 24009491 | Fengwei Zhang | Zephyri.fw@gamil.com |
| **Member 2 (Data Engineer)** | 24009365 | Gang Zhao | 3132057704@qq.com |
| **Member 3 (Interface Developer)** | 24009350 | Tianshuo Gao | 3084083988@qq.com |
| **Member 4 (Agent Developer & Test Engineer)** | 24009323 | Xiao Zhang | 274751389@qq.com |
| **Member 5 (Agent Developer & Test Engineer)** | 24009400 | Wenhan Zhang | 3384155536@qq.com |

---

## Project Overview
This project aims to develop an agentic AI system that automatically monitors product prices across multiple e-commerce retailers. Instead of manually checking websites or subscribing to generic email notifications, users can instruct the system in natural language (e.g., "Notify me when wireless headphones drop below $200") and the autonomous agent will continuously track target items and alert the user when a bargain deal is detected. The system combines web scraping, task scheduling, and large language model (LLM) based natural language understanding to deliver a proactive, personalised shopping assistant.

---

## 2. Motivation and Project Goals
Online consumers spend significant time manually tracking price fluctuations of desired products across multiple retailers. Existing price alert tools often require manual configuration and lack flexible, conversational interaction. This project addresses this pain point by building an intelligent agent that autonomously performs monitoring tasks on behalf of the user.

### Key Goals
- Build an autonomous agent capable of scheduled price monitoring across multiple retail websites.
- Implement natural language interaction so users can define monitoring tasks conversationally.
- Develop a reliable notification system to alert users of price drops in real time.
- Evaluate the system accuracy for both price extraction and instruction understanding.
- Deliver a working Python prototype with clear documentation.

---

## 3. Related Work and Novelty
The concept of shopping agents dates back to early work by Doorenbos et al. [1], who developed ShopBot — a domain-independent comparison-shopping agent that used heuristic search and pattern matching to extract product prices from multiple vendors. Maes et al. [2] further surveyed how software agents transform e-commerce by automating buying and selling processes. More recently, Jorge et al. [3] demonstrated practical web-scraping pipelines for competitive price monitoring in the wine retail sector, validating the industrial feasibility of automated price tracking.

While traditional price monitoring tools focus on structured data extraction, this project is unique in integrating modern agentic AI design with LLM-powered natural language interaction. The novelty lies in:
1. Users can define monitoring tasks using plain natural language instead of manual form-filling.
2. The agent operates autonomously with task scheduling, threshold judgement, and notification dispatch forming a closed loop.
3. The system is designed to be adaptable across different retail platforms with minimal configuration.

---

## 4. Proposed Method and Technical Approach
The system will be built using Python and consists of four core modules:
- **Web Scraping Module**: Uses Requests + BeautifulSoup for static pages and Selenium for JavaScript-rendered dynamic pages. XPath and regular expressions extract product names, current prices, and promotional information. Anti-crawling measures such as request throttling and user-agent rotation are implemented to ensure stable data collection.
- **Agentic AI Core Module**: Integrates an LLM API to parse natural language user instructions into structured monitoring tasks (product URL, target price threshold, check frequency). A task scheduler (using the `schedule` library) runs periodic price checks and triggers notifications when the price drops below the user-defined threshold. This forms the autonomous execution loop of the agent.
- **Notification Module**: Implements dual-channel notification via SMTP email and third-party SMS API. A retry mechanism and fallback logic ensure delivery reliability.
- **User Interface**: A command-line interface (with optional lightweight web UI) allows users to add, remove, and manage monitoring tasks.

---

## 5. Evaluation Plan
System accuracy will be evaluated across three dimensions, following the project requirement for quantitative assessment:
- **Price Extraction Accuracy**: Compare automatically scraped prices against manually verified prices across 20+ products from 3+ retailers. Measure precision and recall of correct price detection.
- **Natural Language Instruction Parsing Accuracy**: Test 50+ user commands of varying complexity (simple threshold, multi-condition, colloquial expressions) and measure how many are correctly parsed into valid monitoring tasks.
- **Notification Trigger Accuracy**: Measure the rate of correct alert dispatch (true positives) versus missed deals or false alarms across a one-week continuous monitoring test.

---

## 6. Significance of the Project
Once this agentic AI monitoring system is fully implemented, it delivers practical value for both end users and technical research:
- **For consumers**: The system eliminates the repetitive manual price-checking work across multiple retail websites. Users only need to input natural language requirements, and the agent will automatically track goods and send instant alerts when prices drop below preset thresholds, greatly saving time and helping users capture limited-time discounts rationally.
- **For technical practice**: This project verifies the practical integration scheme of agentic AI, LLM natural language parsing and web scraping in e-commerce consumer scenarios. It provides a complete development template for similar autonomous monitoring agents, and lays a foundation for extended functions such as multi-merchant price comparison and coupon matching in follow-up research.
- **For domain exploration**: It enriches the application cases of lightweight agentic AI systems oriented to ordinary users, proving that intelligent agents can provide active, personalized service instead of passive information push.

---

## 7. Estimated Timeline

| Timeframe | Tasks |
| :--- | :--- |
| **Weeks 1-2** | Requirements analysis, technology selection, development environment setup, and team role finalisation. |
| **Weeks 3-4** | Web scraping module development and testing across target retail platforms. |
| **Weeks 5-6** | LLM instruction parsing integration and agent task scheduling core development. |
| **Weeks 7-8** | Notification system and user interface development; initial end-to-end integration. |
| **Weeks 9-10** | System evaluation, accuracy testing, bug fixing, and performance optimisation. |
| **Weeks 11-12** | Final documentation, user manual preparation, and project presentation/demo. |

### Deliverables
- Fully functional Python prototype with source code and setup instructions
- System evaluation report including accuracy metrics and test results
- User manual and technical documentation
- Final project presentation and demo

---

## 8. Team Responsibilities
- **Project Lead (Member 1)**: Project progress oversight and milestone management; overall architecture design and System Context Diagram development; supervisor liaison; full system integration and final documentation consolidation.
- **Data Engineer (Member 2)**: Static & dynamic webpage crawler development, anti-crawl mechanism, price database construction; assist with data accuracy test cases.
- **Interface Developer (Member 3)**: Email & SMS notification module, user interaction interface, task management functions; jointly test notification trigger logic with Member 4.
- **Agent Developer & Test Engineer (Member 4)**: Collaborate with Member 5 on LLM natural language parsing, autonomous monitoring loop implementation, and task scheduling; jointly conduct system testing and accuracy evaluation with Member 1 & 4; jointly verify Agent parsing and price threshold judgment accuracy.
- **Agent Developer & Test Engineer (Member 5)**: Collaborate with Member 5 on LLM natural language parsing, autonomous monitoring loop implementation, and task scheduling; jointly conduct system testing and accuracy evaluation with Member 1 & 4; jointly verify Agent parsing and price threshold judgment accuracy.

---

## 9. References
- **[1]** Doorenbos, R. B., Etzioni, O., & Weld, D. S. *A Scalable Comparison-Shopping Agent for the World-Wide Web*[C]//Proceedings of the First International Conference on Autonomous Agents (AGENTS '97). ACM, 1997: 39-48.
- **[2]** Maes, P., Guttman, R. H., & Moukas, A. G. *Agents that Buy and Sell: Transforming Commerce as We Know It*[J]. Communications of the ACM, 1999, 42(3): 81-91.
- **[3]** Jorge, O., Pons, A., Rius, J., et al. *Increasing online shop revenues with web scraping: a case study for the wine sector*[J]. British Food Journal, 2020, 122(11): 3383-3401.
