# Academic and practitioner evidence: returns after order/contract wins and after earnings (PEAD), India first

Context for the reader: our own tests (NSE mainboard 2018-2022, liquidity >= Rs 5 cr/day, entry at close of day+1, 60 trading days vs industry, 50 bps cost) found no edge after large order wins (>= 10% of trailing revenue). A results-surprise edge passed in-sample but failed the 2023-24 check. The evidence below is mostly consistent with that.

Note on method: about 20 search/fetch calls. Several primary PDFs were read in full (the Nifty 500 PEAD paper, Brochet-Lee-Srinivasan, the TenderAlpha/HKU defence white paper). Others (the Korean, Chinese and Indian infrastructure order studies, the SEBI/RBI SME material) are known only from abstracts or press summaries because the full text was paywalled or returned 403. Those are flagged.

## 1. Indian event studies on order/contract wins (defence, railway, infrastructure, government)

### Takeaway
I found no rigorous, large-sample Indian event study of order-win announcements. The only India-specific paper found is a small, low-tier study of 15 infrastructure companies after COVID. Indian evidence on order wins is effectively absent, so our own null result (2018-22) is probably the largest test of this question for India.

### Cited Findings
- "Impact of Order Announcements on Share Prices: A Post-COVID-19 Study of Selected Indian Infrastructure Development Companies": 15 infrastructure companies, BSE data, prices from investing.com, three event windows. The abstract reports minimal abnormal returns well before announcements, a moderate positive jump in the short window, and a "more pronounced sustained gain" afterwards. Full text was blocked (403), so I could not check event counts, the size of the CARs, significance, or controls. Small sample, not peer-reviewed at a strong venue, post-COVID bull market only. — [RR International Journal of Multidisciplinary](https://rrjournals.com/index.php/rrijm/article/view/1823)
- Searches for railway- or defence-specific Indian order-announcement event studies returned only trade-news items (e.g. Titagarh Rail's Rs 1,717 cr order from the Ministry of Railways; DSIJ "order book" articles), with no return analysis. — [Trendlyne](https://trendlyne.com/posts/5915950/market-opens-flat-titagarh-rail-wins-a-rs-1717-crore-order-from-the-mor); [DSIJ](https://insights.dsij.in/dsijarticledetail/rs-83221-crore-order-book-railway-infrastructure-company-receives-order-worth-rs-180009681008-from-east-central-railway-id001-43757)
- Indian event studies of other corporate announcements (buybacks) often find no significant abnormal returns, unlike in developed markets. Example: Chatterjee & Dutta (2015, Vikalpa) on open-market repurchases. — [Vikalpa / SAGE](https://doi.org/10.1177/0256090915611773)

### Inferences
- The lack of Indian literature, together with our 2018-22 null, suggests there is no published, documented order-win drift in India to fall back on. The financial press calls order-win stocks "multibaggers", but that is survivorship-heavy storytelling, not evidence.
- The international results (section 2) point to the pattern you would expect for India: any reaction is short (1-5 days), concentrated where the order is large relative to the firm's size, and partly priced in beforehand. A 60-day hold entered at close of day+1 is the wrong horizon to catch that kind of effect.

### Gaps
- No Indian study measures pre-announcement leakage ahead of order wins (e.g. day -10 to -1 run-up). Our own data is the only way to get this.
- I found no Indian study that separates government or PSU contracts (defence, railways) from private orders.

## 2. International evidence on contract-award announcements

### Takeaway
The best large-sample evidence (US defence, 16.5k announcements, 2010-2025) finds that the average contract announcement carries almost no information: 3-day returns are about +0.6 bps value-weighted and +4 bps equal-weighted, both insignificant. Returns are meaningful only for awards in the top 5% by size relative to market cap or assets: about +26 to +30 bps over 3 days (t of about 2.0-2.4), and that is gross of costs. Korean and Chinese studies find a positive announcement-day jump, a pre-announcement run-up, and quick reversal or overreaction rather than drift.

### Cited Findings
**US defence contracts: TenderAlpha + HKU white paper (dated 2026-01-10). Vendor research, not peer-reviewed, but with clear method.**
- Sample: 2010-01-01 to 2025-05-31; 16,529 contracts, 183 listed firms; mean contract USD 287m, median USD 19m. Dominated by large primes (LMT 3,162 events, RTX 1,985, BA 1,730). Entry at the close of the first tradable day after the announcement; market-adjusted returns. "All results are gross of transaction costs." — [TenderAlpha/HKU white paper](https://www.tenderalpha.com/wp-content/uploads/2026/02/White-Paper-Defense-Contract-Announcements-TenderAlpha-HKU-1.pdf)
- All announcements: 3-day VW +0.6 bps (t 0.18), EW +4 bps (t 1.2); 5-day VW +3.4 bps (t 0.76), EW +7.5 bps (t 1.75); N = 16,491. — same source
- Absolute contract size: top 5% gives 3-day EW +13.6 bps (t 1.33) and 5-day EW +12.8 bps (t 0.99), not significant. — same source
- Size relative to market cap: top 5% gives 3-day VW +30 bps (t 2.35), EW +28.3 bps (t 2.18), N = 1,006; at 5 days VW +23.1 bps (t 1.43). The return does NOT grow from 3 to 5 days, so there is no extended drift. Relative to assets, top 5% gives 3-day VW +28.1 bps (t 2.24). — same source
- New awards vs modifications: new awards 3-day EW +0.1 bps (t 0.02) and 5-day +3.2 bps (t 0.67). Modifications 5-day EW +12.8 bps (t 2.07). New awards carry almost no news. — same source
- Short-duration contracts (1 year or less): 5-day VW +28.9 bps (t 2.82). Army awards: 3-day EW +13.3 bps (t 1.97). Overlapping portfolios have gross Sharpe ratios of 0.55-1.13 with a median of only 1-2 active cohorts, so these are thin, gross-of-cost books. The authors note that large awards are "partially anticipated". — same source
- Caution: many subset cuts were run (size, agency, duration, type), with t-stats around 2. This is a multiple-testing problem, and it is the kind of slicing our protocol would discount.

**Korea: single sales/supply contract disclosures (단일판매·공급계약 공시)** (from the abstract/search summary only; full text not read)
- 2006-2011, 6,072 disclosures, 982 firms. Significant positive announcement-day return and higher volume afterwards. Abnormal returns are already positive BEFORE the disclosure and reverse the next day; the authors raise insider-trading concerns. — [DBpia (Korean Business Research article)](https://www.dbpia.co.kr/journal/articleDetail?nodeId=NODE10777034); [Kyobo Scholar](https://scholar.kyobobook.co.kr/article/detail/4010025096430)
- A KOSPI thesis (2009-2020) finds abnormal returns peak on the disclosure date and then decline (overreaction), with less overreaction at firms with strong ESG scores. — [RISS](https://m.riss.kr/search/detail/DetailView.do?p_mat_type=be54d9b8bc7cdb09&control_no=16a2d6b3f855fb48ffe0bdc3ef48d419)

**China**
- "The immediate impact of purchasing/sales contract announcements on the market value of firms: An empirical study in China" (Int. J. Production Economics, 2014): per the search summary, 318 announcements, mean abnormal return about +1.21% (5% level). This is an announcement-window effect only. — [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0925527314001856)
- "When Public Demand becomes Bad News: Government Procurement and Stock Market Reactions" (He, Hsieh, Luo, Wu, SSRN): government procurement wins can be NEGATIVE news. Losses are concentrated in high Tobin's Q suppliers; low-Q suppliers gain. — [SSRN 7573758](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=7573758)

**Other**
- Corporate contracting (UCLA Anderson WP): contractors show positive announcement-period abnormal returns, rising with contract length. — [UCLA Anderson WP 20-05](https://www.anderson.ucla.edu/documents/areas/fac/finance/20-05.pdf)
- NASDAQ pharma federal contract wins: CARs are positive, but average abnormal returns within the window are insignificant, and the authors call the effect "limited". — [Journal of Economic Analysis](https://www.anserpress.org/journal/jea/3/1/43)

### Inferences
- Across markets, the pattern is consistent: (a) a run-up before the announcement (leakage or anticipation); (b) a one-day jump; (c) reversal or flat after that, not a 60-day drift. The only robust conditioner is materiality relative to firm size. We already used that (>= 10% of revenue) and still found nothing at 60 days.
- At about 25-30 bps over 3 days gross, the best US subset would not survive a 50 bps round-trip cost. In India, the day-0 gap would mostly be priced at the open, before a close-of-day+1 entry.

### Gaps
- No full text was read for the Korean or Chinese papers, so their exact magnitudes and windows are unverified beyond the abstracts.
- I found no peer-reviewed US paper on DoD awards with long-horizon (30-60 day) drift tests.

## 3. PEAD in India: size, persistence, decay, where it lives, SUE vs revenue

### Takeaway
Indian PEAD is documented, but only in older samples: about 4-6% top-minus-bottom SUE decile over days +2 to +64 (Nifty 500, 2002-2017), and a significant 30-day drift in 2006-2014 annual-earnings data. No peer-reviewed Indian study covers 2018 onward. The only evidence on decay is US practitioner work (2023-26), which shows the drift shrinking sharply and becoming marginal after costs outside micro caps. Our failed 2023-24 out-of-sample check fits that picture.

### Cited Findings
**Nifty 500, 2002 Q4 to 2017 Q3 (Scientific Research / Theoretical Economics Letters, 2018)**
- Universe: Nifty 500 constituents. SUE = (EPS minus same quarter last year's EPS) / price 6-12 days before; decile sorts; top and bottom 1% trimmed. PEAD = buy-and-hold return days +2 to +64 minus the Nifty 500. Number of announcements not reported. — [SCIRP full text](https://file.scirp.org/Html/20-1501629_88060.htm)
- Top-minus-bottom decile: full period +4.8% (P1 -1.4%, P10 +3.4%); 2002-08 +5.7%; 2008-17 +4.2%; all significant at 1%. Fama-MacBeth slope on coded SUE: 0.06 full, 0.07 and 0.05 in the two sub-periods (about 6% over 64 days). — same
- Most of the spread is on the LONG side (P10 +3.4% vs P1 -1.4%), and the short side weakens in 2008-17 (P1 only -0.5%). — same
- Conditioners: SUE x P/B positive (the effect is larger in high-P/B stocks); SUE x Beta negative (larger in low-beta stocks). SUE x Size, SUE x Amihud illiquidity and SUE x arbitrage risk are insignificant over the full period. Only in 2008-17 is SUE x Size significant (-0.06, so larger in smaller firms). Transaction costs were not tested. Day-0 returns were not measured (the window starts at day +2). — same

**Brochet, Lee & Srinivasan (2017 draft, NYU Stern-NSE initiative): India, 2006-2014, annual earnings**
- About 2,900 firms (Prowess + Compustat Global, which tilts toward larger firms), 19,403 firm-years. 3-day announcement CAR: 1.95% higher for the top vs bottom SUE decile (coefficient 0.195%/decile). — [Brochet, Lee, Srinivasan PDF](https://www.stern.nyu.edu/sites/default/files/assets/documents/Brochet%20Lee%20Srinivasan%202017%20-%20The%20Role%20of%20Insider%20Trading%20in%20the%20Market%20Reaction%20to%20Earnings%20News.pdf)
- PEAD: a significant SUE coefficient on 30-day CAR starting day +2, so drift exists. There is NO drift after announcements preceded by insider purchases, and stronger drift after those preceded by insider sales. Using I/B/E/S analyst-based SUE cuts the sample by about 75%, which shows how thin analyst coverage is in India. — same
- Insiders net-bought before 5.8% of firm-year announcements and net-sold before 2.6%. When insiders bought, the 3-day announcement CAR was 1.14% higher. Median 5-day CAR around disclosure of an insider purchase is +0.59%; for a sale, -0.41%. — same

**Krishnan & Rangan, NSE-IGIDR WP 4 (2014-15)**
- More than 18,000 quarterly earnings announcements; finds PEAD in India and that post-announcement insider trading is positively related to the surprise. Insiders cut trading in the weeks before results and trade heavily during and after them, which the authors read as "laxity in enforcement". The full text timed out, so magnitudes were not verified. — [NSE-IGIDR WP4-14-15](https://nsearchives.nseindia.com/research/content/NSE-IGIDR-WP4-14-15.pdf)

**Other Indian studies (weaker)**
- Turnaround firms, 49 firms, 2004-2009: PEAD found, but "a large amount of drift occurred within 5 days". Very small sample. — [ResearchGate](https://www.researchgate.net/publication/228258878_EMH_and_Post-Earning_Announcement_Drift_An_Insight_from_Event_Study_of_Turnaround_Companies_in_India)
- 100 NSE firms, 2014-2018: a NEGATIVE association between abnormal returns and earnings surprise, i.e. no standard PEAD. Small sample. — [EconStor](https://www.econstor.eu/handle/10419/308924)
- "Earnings surprise and sophisticated investor preferences in India" (Pacific-Basin / ScienceDirect): "very little evidence" that transient institutional investors exploit the surprise mispricing. — [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S1815566909000034)
- Hung, Li & Wang (global, IFRS adoption as an information shock): better reporting quality reduces PEAD. Their India row could not be read. — [HKUST IEMS WP 2015-17](https://iems.ust.hk/assets/publications/working-papers-2015/iemswp2015-17.pdf)

**Decay and costs (US, practitioner, 2023-2026; not India)**
- 43,111 US announcements, 4,058 firms, Jan 2023 to Oct 2026; filters of price >= $5 and >= $1M/day volume. SUE top-minus-bottom quintile over days +2 to +61: +1.7 pp overall (t 4.2), falling from +2.6 pp (2023-24) to +0.5 pp (2025-26). Small caps ($300M-2B): +1.1 pp in total, +2.2 in 2023-24, then -0.2 in 2025-26. Micro caps (< $300M): +6.2 pp (t 1.8). After costs: the mid/large book goes from 7.9%/yr gross to 2.1% at 30 bps; the small book goes from 6.6% to 1.8% at 25 bps (Sharpe 0.26); the micro book is 20.8% at 50 bps but with a -31% drawdown. — [guptagaurav.info blog](https://guptagaurav.info/articles/earnings-drift)
- Quantpedia's summary of the classic US PEAD research: the main contributors are small-cap stocks, so caution is warranted. — [Quantpedia](https://quantpedia.com/strategies/post-earnings-announcement-effect)
- Livnat & Mendenhall (NYU): revenue surprise adds to EPS surprise in predicting drift (US evidence). Drift is stronger when both surprises agree. — [Livnat PDF](https://pages.stern.nyu.edu/~jlivnat/drift%20revenue%20and%20earnings.pdf)

### Inferences
- India's published PEAD (about 4-6% over 64 days) comes from 2002-2017, before Indian markets saw heavy retail and algorithmic participation and before results were spread instantly through apps and social media. A drop from the 2002-08 to the 2008-17 sub-period (5.7% to 4.2%) is already visible. The US decay seen in 2025-26 suggests the published Indian figure probably overstates any current edge. That is consistent with our 2023-24 out-of-sample failure.
- The Indian drift was mostly on the long side, in high-P/B and low-beta names, and was not explained by illiquidity in the 2002-17 sample. Pre-announcement insider buying removes the drift, which suggests that information which leaks or is anticipated is already in the price.
- Our test (liquidity >= Rs 5 cr/day, 50 bps cost, 60 days, industry-relative) is stricter than the academic set-ups, which use the market as benchmark, no costs, and entry at day +2. A gross 4% spread split into long-only, industry-relative returns could fall to around 1-2%, which is close to the cost of trading.

### Gaps
- No peer-reviewed Indian PEAD study covering 2018-2025 was found, and no Indian decay study.
- No Indian study compares SUE with revenue surprise, or conditions on analyst coverage (beyond the BLS note that I/B/E/S covers about 25% of firms).
- No Indian study splits day-0 returns from drift for quarterly results with recent data.

## 4. SME platforms (NSE Emerge, BSE SME): IPO returns, manipulation, liquidity, announcement effects

### Takeaway
The evidence on SME stocks is about IPO pricing and manipulation, not about announcement drift. Regulators (SEBI, RBI) describe large listing-day gains followed by quick reversals, and promoters using "positive announcements" to pump prices before selling. In this segment, an order-win announcement is as likely to be a manipulation tool as real news. No study of a tradable announcement effect on SME platforms was found.

### Cited Findings
- RBI staff study (reported October 2025): recent SME IPOs show sharp listing gains followed by negative returns within a short period. Full article blocked (403); numbers not verified. — [Business Standard](https://www.business-standard.com/markets/ipo/recent-sme-ipos-show-sharp-listing-gains-quick-reversal-rbi-study-125102100487_1.html)
- SEBI chair (March 2024): signs of price manipulation in the SME segment, both at the IPO stage and in trading. — [Business Standard](https://www.business-standard.com/amp/markets/news/seeing-signs-of-price-manipulation-in-sme-segment-sebi-chief-buch-124031100457_1.html)
- SEBI advisory (August 2024): promoters make positive announcements, follow them with corporate actions (bonus issues, splits) to lift prices, then sell their own holdings. — [Business Standard](https://www.business-standard.com/amp/markets/capital-market-news/sebi-raises-alarm-on-manipulation-in-sme-segment-124082900541_1.html)
- A SEBI order against an NSE Emerge company (2024): the company made announcements that "paint a rosy picture" with no real economic activity; the promoter diverted IPO funds. — [Business Standard](https://www.business-standard.com/markets/stock-market-news/sebi-cracks-whip-on-second-sme-bars-promoter-for-diverting-ipo-funds-124051001002_1.html)
- January 2026 order in an SME stock case (DU Digital Global): 26 individuals barred, Rs 1.85 cr penalty, disgorgement of more than Rs 98.78 lakh for circular trading. SEBI found no corporate announcements that justified a 2,467% one-year price rise. — [Business Standard](https://www.business-standard.com/amp/markets/news/sebi-bars-26-individuals-imposes-1-85-cr-penalty-for-manipulation-126010100719_1.html)
- NSE capped SME IPO opening prices at 90% above issue price (July 2024), and SEBI extended the ST-ASM surveillance framework to SME stocks. — [Business Standard](https://www.business-standard.com/markets/ipo/nse-imposes-cap-on-price-of-sme-debutants-amid-concerns-of-manipulation-124070400486_1.html); [Upstox](https://upstox.com/learning-center/online-trading/bse-sme-ip-os/article-1562/)
- An academic analysis of SME IPOs (underpricing, aftermarket liquidity, long-run underperformance) found that issuer age and subscription rate do not explain long-run underperformance. Low-tier paper; details not verified. — [ResearchGate](https://www.researchgate.net/publication/396285518_ANALYSIS_OF_PERFORMANCE_OF_RECENT_SME_IPOS_LISTED_ON_NSEx_AND_BSEx)
- Fixed (large) lot sizes in SME trading make exits hard when prices fall. Commentary-level source. — [Business Standard / press coverage via search](https://www.business-standard.com/amp/markets/capital-market-news/sebi-raises-alarm-on-manipulation-in-sme-segment-124082900541_1.html)

### Inferences
- In SME stocks, liquidity, lot-size frictions and announcement-driven pumps rule out a tradable, verifiable announcement signal. An apparent effect in a backtest would most likely reflect manipulation episodes that later reverse. Our Rs 5 cr/day liquidity floor already excludes almost all of this segment.

### Gaps
- No peer-reviewed study of corporate-announcement returns (orders or results) on NSE Emerge or BSE SME was found.
- The SEBI consultation-paper data (2026 review of the SME framework) could not be read directly.

## 5. Information leakage and insider trading before announcements in India (SEBI PIT)

### Takeaway
There is direct evidence that Indian insiders trade before and around earnings, and that their pre-announcement buying absorbs both the announcement reaction and the drift. Enforcement is slow and weak: SEBI insider-trading orders do not even move prices. Order wins are classic unpublished price-sensitive information (UPSI). Leakage is a likely reason the post-announcement window is empty, but no Indian study measures leakage ahead of order wins specifically.

### Cited Findings
- Krishnan & Rangan (NSE-IGIDR): insiders reduce trading in the weeks before results and trade sharply more during and after them. The authors read this as "laxity in enforcement" of the trading-window rules. — [NSE-IGIDR WP4-14-15](https://nsearchives.nseindia.com/research/content/NSE-IGIDR-WP4-14-15.pdf)
- Brochet, Lee & Srinivasan (2006-2014): pre-announcement insider purchases preempt part of the earnings news (the response to earnings is lower when insiders traded beforehand) and eliminate PEAD. Insiders trade in an informed way mainly in non-conglomerate, non-government firms with low FII ownership. There is little evidence of informed reporting-insider trades before M&A, although target run-ups exist: mean 30-day pre-announcement target return of 5.89% vs a 2.33% announcement return. — [BLS PDF](https://www.stern.nyu.edu/sites/default/files/assets/documents/Brochet%20Lee%20Srinivasan%202017%20-%20The%20Role%20of%20Insider%20Trading%20in%20the%20Market%20Reaction%20to%20Earnings%20News.pdf)
- Gupta, Patel & Sane (The Leap Blog, May 2026): 176 SEBI final insider-trading orders and 42 SAT decisions, 2009-2023, NSE firms. There is no significant CAR (-10 to +10) for any type of order. Reasons given: 30-41% of orders are appealed and about 50% of those are modified or reversed; a median of more than 3 years from violation to show-cause notice plus about 18 more months to a final order; median penalty Rs 12.83 lakh. — [The Leap Blog](https://blog.theleapjournal.org/2026/05/market-reaction-to-insider-trading.html)
- Korean contract disclosures show positive abnormal returns BEFORE the disclosure, with explicit insider-trading concerns (abstract only). — [DBpia](https://www.dbpia.co.kr/journal/articleDetail?nodeId=NODE10777034)

### Inferences
- With weak enforcement, and with order wins visible to customers, bidders and employees before disclosure, a pre-announcement run-up in India is plausible, leaving little news by close of day+1. This is worth checking directly in our data by measuring CAR from day -20 to -1 in the order-win sample. That would be an exploration row, not a new strategy test.

### Gaps
- No Indian study of leakage specifically before order or contract announcements.
- No systematic analysis of SEBI PIT orders by type of price-sensitive information (orders vs results vs M&A).

## 6. Broker or quant notes testing such strategies with numbers

### Takeaway
I found no public Indian broker note (Kotak, Motilal, Ambit, Edelweiss) that backtests order-win or PEAD strategies with numbers. The public quant material found is either US-based (guptagaurav.info, TenderAlpha, Quantpedia) or generic backtesting advice.

### Cited Findings
- US PEAD practitioner backtest, 2023-26: net returns are modest and fragile outside micro caps (numbers in section 3). — [guptagaurav.info](https://guptagaurav.info/articles/earnings-drift)
- TenderAlpha defence-contract backtests: gross Sharpe 0.55-1.13 for selected subsets, explicitly gross of costs. — [TenderAlpha/HKU](https://www.tenderalpha.com/wp-content/uploads/2026/02/White-Paper-Defense-Contract-Announcements-TenderAlpha-HKU-1.pdf); Boeing framework-contract (IDV) case study: [TenderAlpha blog](https://www.tenderalpha.com/post-award-announcement-drift-effect-of-idv-framework-events-on-the-share-price-of-boeing-corp/)
- Indian backtesting guidance warns that in thin small caps an order can move the price or fail to fill, which inflates small-cap drift backtests. — [AlphaQuark](https://alphaquark.in/blog/how-to-backtest-stock-strategy-india)
- A Stockmaniacs blog claims Indian mid/small caps "continue to exhibit stronger and more persistent drift", with no data behind it. Unreliable. — [Stockmaniacs](https://www.stockmaniacs.net/post-earnings-announcement-drift/)

### Inferences
- Indian sell-side "results surprise" and "order inflow" commentary is stock-specific narrative, not a systematic test. Its absence from public research is weak evidence that brokers have not found, or do not publish, a robust systematic edge.

### Gaps
- Broker strategy notes are mostly behind client logins and were not accessed (per the constraint not to log in).
