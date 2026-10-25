# Custom Stock-Selection Frameworks: Indian Market Investors on X

Oct 23, 2026 · @Dev

## Scope

Seven investors with a written, repeatable stock-selection method, each reconstructed from their own decks, memos, books, podcasts and posts on X. Generalist commentators (macro, personal finance, broad technical or fundamental talk) are left out on purpose.

"Custom method" here means: a stated universe, a stated filter or score, a stated portfolio rule, and a stated exit or rebalance rule. Where a step is not public, the gap is named rather than guessed.

Context: under SEBI's 2024 amendments only registered Investment Advisers (RIAs) and Research Analysts (RAs) may recommend specific securities on social media, and registered entities may not associate with unregistered finfluencers. Every investor below runs a SEBI-registered PMS, AIF or AMC, so what they post on X is framework and portfolio commentary, not calls. None of this is investment advice.

## Deepak Shenoy / Capitalmind — Adaptive Momentum (X: @deepakshenoy, @capitalmind\_in)

Capitalmind runs the longest live momentum book in India: the PMS went live 5 Mar 2019, one rupee at inception was worth 3.53 after five years versus 2.35 for the NSE 500, and the same engine now sits inside the Capitalmind Flexi Cap mutual fund (NFO Jul 2025). Shenoy's own background is algorithmic trading (he started one of India's first algo firms in 2009); the quant work is run by Anoop Vijaykumar (CIO) and Prashanth Krishna (head of trading). What follows is the pipeline as they have published it.

**Paradigm.** Price is the only signal. They are not asking why a stock is rising, only whether it is, and whether it has stopped. Momentum is a bet with the trend, never contrarian. Alpha, in their words, comes from behaviour (following the rules through drawdowns) rather than from the cleverness of the model, which they describe as two to four variables with price as the key one.

**Step 1 — Universe.** All NSE-listed stocks that pass an average-traded-value liquidity filter. In practice that is roughly the top 500 by market cap with a few exceptions. Liquidity is the first and non-negotiable filter: manipulation and pump-and-dump are low-volume phenomena, so a hard volume floor removes most of them; the rest is handled by diversification. Stocks that keep hitting upper or lower circuits are excluded.

**Step 2 — Scoring.** Each stock gets a composite momentum score. Conventional momentum uses a single point-to-point return (e.g. 12-month minus 1-month); Capitalmind uses a composite of returns across lookbacks adjusted for volatility, so a stock that rose 40% in a straight line outscores one that rose 40% in 20% daily swings. The exact lookback weights are proprietary. In the 2019 podcast they describe a one-year lookback and a preference for the stock that goes up steadily without making waves.

**Step 3 — Entry filters.** Short-term price and volume criteria screen out stocks that have just gone parabolic, because, as Krishna put it, a parabolic rise only rewards with a parabolic fall and lower circuits stop you from exiting (HEG and Vakrangee are their worked examples). This step is what distinguishes Adaptive Momentum from a plain top-decile sort.

**Step 4 — Selection and sizing.** Take the top decile by score and hold 25–30 names, equal-weighted (3.3–4% each). The sizing logic is explicit: a 50% fall in one name costs under 2% of capital, which is survivable even when the backtest's exit was impossible in reality. Sector and business-group exposure is capped.

**Step 5 — Rebalance.** Monthly. They tested daily to quarterly; monthly balanced signal freshness against costs, taxes and slippage. Between rebalances a holding is left alone unless there is fraud-level news. Expected holding period averages a couple of months.

**Step 6 — Cash rule.** If fewer than 30 stocks qualify, the empty slots are held in cash: find 20, and you are 33% in cash. In 2008-style declines the book went fully to cash. Separate portfolio-level rules move partially or fully out of equities when breadth weakens. This is the "adaptive" part and why drawdowns have historically tracked the Nifty rather than the small-cap index, even though the book is 50% or more mid and small cap in bull phases and drifts to large caps in bear phases.

**What they tell you not to expect.** Their own FAQ lists five reasons not to invest: high churn; nearly half of entries exit in the red (returns come from winning more on winners than losing on losers); it will hold "ugly" stocks with no story; it gets written off periodically; and it trails the index on a one-year basis about a third of the time. The 2019 target was 18–24% annualised over a cycle; live numbers as of 22 Oct 2026 are 17.2% over 7 years versus 11.4% for Nifty 50 TRI, with a 2-year stretch of underperformance (-7.4% vs -2.6%).

**Resilient (sister strategy, Sep 2021).** Same machinery, different factor: a composite of price volatility and fundamental profitability, top decile, rebalanced quarterly. It gravitates toward liquid large and mid caps that are not in the news. Shenoy frames the two as complements: momentum for participation, Resilient for the steady core.

**Flexi Cap fund (2025).** Momentum remains the primary factor, but a factor-timing layer switches to low-risk, quality or value when quantitative signals say risk is rising, and predefined hedging rules use index derivatives in rough markets. The rules were tested on 15 years of data; the benchmark is Nifty 500 TRI; they state outright there will be one-, three- and five-year stretches of underperformance.

**What is public vs proprietary.** Public: universe, liquidity floor, equal weight, 25–30 names, monthly cadence, cash rule, sector caps, volatility-adjusted composite. Proprietary: the lookback weights, the exact entry-filter thresholds, and the breadth signal that triggers cash. Capitalmind's SSRN paper ("Does Momentum Investing work in India") and the "Demystifying Momentum" paper are the fullest public descriptions.

**On X.** Shenoy posts macro, RBI and bond-market commentary far more than stock talk; the strategy content lives on capitalmind.in and the podcast. Krishna's handle (@Prashanth\_Krish) is where live momentum-book observations appear.

Sources: [Adaptive Momentum page](https://www.capitalmind.in/portfolio-management-service/momentum) · [Momentum podcast transcript, Aug 2019](https://premium.capitalmind.in/2019/08/podcast-momentum-the-anomaly-that-persists/) · [Resilient page](https://www.capitalmind.in/portfolio-management-service/resilient) · [Flexi Cap launch](https://www.business-standard.com/amp/finance/personal-finance/capitalmind-flexi-cap-fund-debuts-data-led-momentum-driven-investing-125071800749_1.html) · [Flexi Cap draft filing](https://www.angelone.in/news/mutual-funds/capitalmind-mutual-fund-files-draft-for-flexi-cap-fund)

## Alok Jain / WeekendInvesting — rotational and absolute momentum (X: @WeekendInvestng)

Jain runs the most retail-facing rules-based momentum shop in India: a family of smallcases live since 2016, a SEBI RIA, and since 2025 a Category III AIF (Winvest Capital, "MEGA Fund"). He set up one of the first NSE memberships in Delhi in 1996, is an IIT Delhi (1991) and University of Maryland (MBA 1995) graduate, and founded WeekendInvesting in 2016 after two decades of running the same rules on his own money. The name is the method: two minutes of work once a week.

**Paradigm: "Bhav Bhagwan Che" (price is God).** Price discounts all information ahead of fundamentals, so the only inputs are price and liquidity. There is zero manager discretion. He describes the rebalance as natural selection: survival of the fittest keeps you out of harm's way as long as you do not interfere. No sector views are ever taken because the ranking auto-selects the strongest sectors.

**Two engines, not one.**

1. *Rotational (relative) momentum* — always fully invested in the top N stocks of a fixed index universe, regardless of market conditions. Beats the benchmark by being in its strongest members on the way up and falling less on the way down. Mi 20, Mi India Top 10, Mi NNF 10 and Mi EverGreen are this type.
2. *Absolute momentum* — a stock must clear an absolute strength threshold to be held; if too few qualify, the slots sit in cash (Liquid Bees) or debt. Mi 30 and the HNI Wealth Builder (Mi 50) are this type. During the Feb–Mar 2020 crash the Mi 50 fell 22% against 50% for the Mid-Small 400, entirely because of the cash rule.

**The product rules, as published on the factsheets.**

| Strategy | Universe | Stocks | Weight | Rebalance | Engine |
| --- | --- | --- | --- | --- | --- |
| Mi 20 | Nifty MidSmallcap 400 (NSE ranks 101–500) | 20 | 5% each at entry | Weekly | Rotational |
| Mi MT Allcap | All NSE stocks with market cap above ₹1,000 cr | 20 | 5%, reset every 6 months (Mar, Sep) | Weekly | Rotational |
| Mi EverGreen | CNX 200 + fixed 25% Gold ETF | 20 | Equal | Monthly | Rotational + hedge |
| Mi India Top 10 | Nifty 50 | 10 | Equal | Weekly | Rotational |
| Mi NNF 10 | Nifty Next 50 | 10 | Equal | Weekly | Rotational |
| Mi ST ATH / Mi ATH 2 | Liquid stocks above ₹1,000 cr / ₹500 cr near all-time or multi-year highs | 10 | Equal | Weekly | Breakout + exit criteria |
| Mi 30 | CNX 500 | up to 33 | Equal | Weekly | Absolute, low churn |
| HNI Wealth Builder (Mi 50) | Mid and small cap | up to 50 | 2% each | Weekly | Absolute, goes to cash/debt |

**Step by step (Mi 20 as the canonical case).** (1) Universe is the index as it stands, so survivorship is handled by NSE. (2) Each week every stock is ranked on a momentum score; Jain has never published the formula, but it is relative strength over a medium lookback, and the ATH products add a distance-from-high condition. (3) Capital goes to the top 20 at 5% each. (4) Winners are allowed to run and are not trimmed, so position weights drift; the Allcap variant resets weights to 5% every six months. (5) At each weekly rebalance a holding is sold only if it has dropped below the exit criterion (fallen out of the top band), and the freed slot goes to the next-highest ranked stock. (6) A special rebalance can remove a stock before the scheduled date on exceptional deterioration. (7) There is no fundamental or governance screen at any step.

**Why weekly.** Jain argues that weekly review avoids deep drawdowns in individual names: a stock that breaks down is out within days rather than weeks. The cost is churn, which the factsheets flag as high. Mi 20 has a published since-inception CAGR of 18.73% (Sep 2026 factsheet) and a return-over-max-drawdown of 0.7 versus 0.3 for its benchmark; the Allcap book shows 22.08% CAGR (Oct 2026 factsheet). A ₹10,000 monthly SIP in Mi 20 from Apr 2016 to Nov 2022 is quoted at 29.6% XIRR versus 11.9% for the index. These are the manager's own figures and are not SEBI-verified.

**What he adds on X and the daily newsletter.** Three recurring arguments: (a) momentum is not short-term — Mi NNF 10 held Zomato from ₹60 to ₹154 over more than a year because the rules never said sell; (b) concentration beats diversification when selection is systematic — Mi India Top 10 was up 14% in two months of 2024 against a flat Nifty; (c) most investors get trapped (his Yes Bank example) because they had no exit plan, and an exit rule is worth more than an entry idea. He also sells a Portfolio Momentum Report that scores an outside portfolio by what share of it is in high- versus low-momentum stocks.

**Known controversies.** Jain's feed is combative; a public exchange with Sushil Kedia in 2023 included accusations of fraud and threats of legal action. This does not bear on the rules, but it is why his handle is polarising.

**What is public vs proprietary.** Public: universe, count, weight, cadence, weight-reset rule, cash rule for the absolute products, the special-rebalance clause. Proprietary: the momentum ranking formula, the exact exit band, and the absolute-strength threshold. Because the universes are index-defined and the counts are fixed, a close approximation (rank on 6- and 12-month return, weekly top-20, equal weight) is easy to reproduce from NSE data.

Sources: [Mi 20 factsheet, Sep 2026](https://assets.smallcase.com/factsheets/WKIMO_0017.pdf) · [Mi MT Allcap factsheet, Oct 2026](https://assets.smallcase.com/factsheets/WKIMO_0004.pdf) · [WeekendInvesting strategies page](https://weekendinvesting.com/strategies-page/) · [Mi 20 strategy page](https://weekendinvesting.com/strategies/mi-20/) · [HNI Wealth Builder page](https://weekendinvesting.com/strategies/mi50) · [smallcase manager page](https://www.smallcase.com/manager/weekend-investing) · [IIFL interview, Sep 2022](https://www.indiainfoline.com/article/news-leader-speak/alok-jain-smallcase-manager-founder-weekend-investing-122092800402_1.html) · [Newsletter, 13 Feb 2024](https://weekendinvesting.beehiiv.com/p/weekendinvesting-newsletter-13-feb-2024) · [Newsletter, 20 Feb 2024](https://weekendinvesting.beehiiv.com/p/weekendinvesting-newsletter-20-feb-2024) · [Winvest Capital AIF listing](https://pmsbazaar.com/AMC/Weekendinvesting-Analytics-Private-Limited)

## Saurabh Mukherjea / Marcellus — forensic accounting, then capital allocation, then moats (X: @saurabhmukherjea, @MarcellusInvest)

Marcellus (founded 2018) runs the most explicitly documented quality process in India: a quantitative two-stage screen that cuts roughly 1,000 companies to a few dozen, followed by bottom-up research to a 12–15 stock book. Mukherjea came to it from Ambit Capital's Coffee Can PMS and from co-founding Clear Capital in London; Rakshit Ranjan manages Consistent Compounders. The method is written up in Coffee Can Investing (2018), The Unusual Billionaires (2016) and Diamonds in the Dust (2021), and the pipeline below is taken from Marcellus's own product decks.

**Paradigm.** Share-price returns over 10 years are explained by earnings growth, not starting P/E. Marcellus's own regression on BSE 100 constituents gives R² of 66% between FY04–19 EPS CAGR and relative share-price CAGR, and under 1% for starting P/E. So the job is to find companies that will compound earnings for a decade and hold them with near-zero churn; valuation is treated as secondary. This is also the root of their 2022–24 failure (below).

**Step 1 — Forensic accounting screen.** Over six years of consolidated financials, every company in the universe is ranked on each of ten ratios (later decks say twelve), and the ranks are summed into an accounting-quality score, then cut into deciles. The published ten:

| Category | Ratio | What it catches |
| --- | --- | --- |
| Income statement | Cash flow from operations as % of EBITDA | Aggressive revenue or earnings recognition |
| Income statement | Volatility in non-operating income | Earnings propped by one-offs |
| Income statement | Provision for doubtful debts vs debtors overdue more than 6 months | Under-provisioning |
| Balance sheet | Yield on cash and cash equivalents | Cash that is not really there |
| Balance sheet | Contingent liabilities as % of net worth | Off-balance-sheet exposure |
| Balance sheet | Change in reserves explained by profit and dividends | Reserves adjusted outside the P&L |
| Auditor | Growth in auditor remuneration vs growth in revenue | Auditor being paid to look away |
| Cash theft | Miscellaneous expenses as % of revenue | Leakage |
| Cash theft | Capital work in progress to gross block | Capex that never becomes an asset |
| Cash theft | Free cash flow (CFO + CFI) to median revenue | Chronic cash burn hidden by accruals |

The framework is adapted from Howard Schilit's Financial Shenanigans. Marcellus's backtest (CY15–CY18) shows the bottom deciles, which they call the Zone of Thuggery, with median returns around -15% to -20% a year and the top deciles, the Zone of Quality, positive. Deterioration in this score is a stated sell trigger: Sterling Tools was exited from Little Champs in 2021 for exactly this reason.

**Step 2 — Capital allocation twin filter.** A company must have delivered double-digit year-on-year revenue growth and return on capital above cost of capital in every single year for ten consecutive years. No averaging and no misses allowed. Their backtest builds this portfolio each year from 2000 and holds it untouched for ten years; every cohort that has completed its run returned 20–30% a year and beat the Sensex by 8–12 points, with volatility over three-plus years that they compare to a government bond. The deck quantifies why no-churn matters: 70% annual churn at 50 bps all-in cost drags the ten-year CAGR by about 120 bps, and deferring LTCG ten years adds about 100 bps.

**Step 3 — Barriers to entry (the "Longevity Framework").** Only the roughly 10% of the universe surviving steps 1 and 2 gets primary research. The questions: does the top one or two players take 70–80% of the sector profit pool (paints, biscuits, adhesives, small cars are their canonical cases); does management obsess over the core franchise rather than adjacent gambles; does the moat visibly deepen over time; does the promoter return excess cash. The output is a quantified view of free-cash-flow growth, revenue drivers and ROCE sustainability, and the 12–15 names with the longest expected runway are bought.

**Portfolio rules.** 12–15 stocks for Consistent Compounders, 15–20 for Little Champs (small caps under ₹75 bn market cap), 10–15 for Kings of Capital (financials). Holding period is described as indefinite; Mukherjea says they rarely look at share prices and rarely buy or sell. There is no formal cash rule, but Little Champs was at about 30% cash in Sep 2025 on valuation grounds.

**What went wrong, in their own words.** All three flagship books underperformed BSE 500 TRI and Nifty 50 TRI since inception as of Aug 2025, and roughly ₹4,000 cr was redeemed over 2022–24. Their Sep 2025 post-mortem on Little Champs lists three causes: (a) staying heavy in top-ten positions despite deteriorating fundamentals and peak valuations, having extrapolated Covid-era market-share gains in chemicals and pharma as structural (the top ten by weight returned a median -21% over Sep 2022–Mar 2024 while the median BSE 500 stock returned +35%); (b) coverage too narrow to find replacements, since conviction takes months to build; (c) impact costs from large positions in illiquid names, the book being under 20 stocks until Oct 2023. Pramod Gubbi's summary: they failed to recognise a regime change and stayed in richly valued quality.

**How the process changed (from early 2024).** Coverage widened to about 90 stocks under ₹75 bn, roughly 3x the portfolio. Earnings are now assessed on a 1–3 year horizon and position sizes are set by the gap between their estimate and consensus. A five-year expected-return model now includes expected change in the multiple, not only earnings growth, and positions are trimmed when price outruns fundamentals; the portfolio's forward P/E has been brought below its long-term average. Positions are smaller and built more slowly. The quant front end (steps 1 and 2) is unchanged.

**MeritorQ (Nov 2022).** A fully quantitative product that packages steps 1 and 2 as rules: forensic screen first, then profit consistency and ROCE thresholds, then a valuation sort, rebalanced periodically. It is the version of the Marcellus method that can be replicated from public data and is sold as a smallcase.

**What is public vs proprietary.** Public: the ten ratios, the decile method, the twin-filter thresholds and look-back, the ten-year no-churn backtest, the portfolio counts, the post-2024 changes. Proprietary: the exact weighting across ratios, the ROCE and growth thresholds used in MeritorQ, and the Longevity Framework scoring.

**On X.** Mukherjea's feed is mostly book excerpts, macro and "India's 20 companies take 90% of profits" style arguments; the method lives in the newsletters at marcellus.in, which are the primary source.

Sources: [Consistent Compounders deck, Jan 2020](https://marcellus.in/wp-content/uploads/2020/01/Marcellus_Creating_Consistent-Compounders_Jan-2020-1.pdf) · [CCP deck, Sep 2023](https://www.sharekhan.com/MediaGalary/Newsletter/CCP-ProductPPT-REGULAR-270923.pdf) · [Little Champs sixth-anniversary post-mortem, Sep 2025](https://marcellus.in/newsletter/little-champs/little-champs-rising-again-on-its-sixth-anniversary/) · [MeritorQ deck](https://marcellus.in/wp-content/uploads/2026/06/mqp-direct-1.pdf) · [Pramod Gubbi interview, Business Standard, Aug 2025](https://www.business-standard.com/amp/markets/interviews/markets-continue-to-ignore-impending-risks-pramod-gubbi-of-marcellus-125083100406_1.html) · [Sterling Tools exit, Money9](https://www.money9.com/news/markets/saurabh-mukherjeas-pms-exits-sterling-tools-over-accounting-standards-53860.html) · [Mukherjea interview, Money9](https://www.money9.com/news/equities/saurabh-mukherjea-and-the-tricks-of-the-trade-43826.html) · [Business Today, May 2024](https://www.businesstoday.in/amp/markets/market-perspective/story/sensex-will-touch-250000-in-10-years-saurabh-mukherjea-of-marcellus-428567-2024-05-07)

## Samit Vartak / SageOne — earnings doubling in 3–4 years, screened by green and red flags (X: @SamitVartak)

SageOne (founded 2012, about ₹5,900 cr AUM and 1,137 clients as of 30 Jun 2026) has the most fully published selection screen of any discretionary manager in India, and Vartak's quarterly memos are the most read fund-manager letters on Indian X. His background is valuation and M&A advisory (Deloitte, E&Y, PwC) rather than broking, which shows in the method: the screen is built around earnings growth, return on incremental capital and a hard "what do I pay" number.

**Paradigm.** Buy the business whose earnings will double in 3–4 years (a 20–25% CAGR), at or below fair value, and hold until one of five exit conditions fires. Vartak's own phrasing: we can afford to get the valuation a little wrong, we cannot afford to get the business wrong, because the first costs a few quarters of returns and the second costs capital permanently. Quality of business and promoter is necessary but not sufficient; what matters is how the company is positioned against competition for the next 3–5 years and whether management executes.

**Step 1 — Universe and fishing pond.** Start from the roughly 2,500 NSE-listed companies with daily trading. Deliberately fish in ranks 101–1,000 by market cap, with a stated sweet spot of ₹5,000–10,000 cr, because the goal is to own the company before it enters the top 100. Large caps are liked for liquidity but rarely pass the growth test.

**Step 2 — Green flags (all should hold).**

1. Earnings growth of 20%+ compounded, with the growth coming substantially from market-share gain or new categories, not from the macro cycle.
2. ROCE and ROE above 20%, and ideally ROE at or above the growth rate (he has quoted 25%), so growth does not require leverage or dilution.
3. Better ROCE on incremental capex than on the existing base.
4. High cash flow from operations: CFO divided by PAT above 1.
5. Sustainable margins through the cycle, which is his test for a non-commodity business.
6. Low leverage.
7. Market-share gains, verified through channel checks with suppliers, customers and competitors and site visits.
8. Favourable ESG practices (added in the GIFT City fund documents).

**Step 3 — Red flags (any one is disqualifying).** Loose accounting policies; poor capital allocation; questionable founder history; related-party transactions; complex corporate structures; corporate governance issues. These are enforced by an in-house financial and forensic model whose ratios are not published.

**Step 4 — What he avoids by category.** Businesses where management does not control pricing: PSUs (regulatory and policy risk), commodity producers (steel is his example, where input cost is fixed and output price is global), and most loss-making tech IPOs. He also warns against buying the demand side of a story without modelling supply: ethanol and 2003–07 infrastructure are his cases where demand was real but low entry barriers meant investors made nothing.

**Step 5 — Valuation.** Fair value is built bottom-up from earnings three years out and a defensible multiple; the Nykaa example is explicit: a 30% growth requirement implied 3x in three years, fair value ₹1.2 lakh cr in three years, so the entry had to be ₹40,000 cr and the IPO price was already the target. He rejects P/E screens as pointless on their own and wants a return that still works if the multiple compresses 30% while earnings double.

**Step 6 — Funnel and portfolio.** 2,500 → 101st–1000th → an active watchlist of about 60 → a portfolio of 12–20 (Core) or 15–25 (Flagship Growth AIF). Weights are conviction-based, and he is candid that conviction is a mix of process and his own qualitative call. Risk caps: at most 25% in related sectors and 20% in one business group. Liquidity is screened at entry because impact cost on a small-cap exit in a downturn can be 30–40%; this is why the small-cap book fell 25% in the Covid crash against 38% for the index.

**Step 7 — Exit framework (five triggers).** (a) Fundamentals deteriorate so the earnings-doubling thesis no longer holds; (b) corporate actions show diminished focus on shareholder value; (c) price has gone beyond fundamentals, with patience; (d) a better risk-reward alternative appears; (e) portfolio-level reduction in a sector or in equity allocation. Average holding period is 3–4 years and 80–90% of names turn over inside five years, so this is not buy-and-forget.

**Track record of the method, in his numbers.** About 80 companies owned over ten years to 2022, roughly 12 got wrong (exited at a loss) and about 50 returned over 25% annualised; stated failure rate under 15% against a market base rate of 25–30%. Named winners: Bajaj Finance (entered 2009), Kaveri Seed (14–15x), PI Industries, Page, Balkrishna, Aarti, APL Apollo, Deepak Nitrite, Navin Fluorine, Polycab, KEI, Laurus. Named miss: Indo Count, where a structural export thesis turned cyclical once Amazon-style channels compressed margins. His stated lesson: in small and mid caps the exit matters more than the entry.

**What the memos on X argue.** Three recurring themes. First, a cycle read built from broad data: in 2021 he analysed roughly 700 companies outside the top 100 over 16 years to argue that mid and small caps had repaired balance sheets more than large caps, but that the coming cycle would not look like 2003–07 because the banking system was cautious. Second, margins versus volumes: he repeatedly separates earnings growth driven by margin expansion (fragile) from growth driven by volume and share (durable), and in 2022–23 warned that post-Covid margin gains would not last. Third, a portfolio balanced between domestic structural growth and export-oriented manufacturers so returns do not depend on one India narrative. His 2025–26 letters focused on small-cap valuations still being above comfort even after the 2025 correction.

**Skin in the game.** Over 90% of Vartak's own wealth sits in SageOne portfolios; employees invest annually through a pooled account. Minimum ticket is ₹2 cr rather than the ₹50 lakh norm, a deliberate filter for investors who can sit through 25%+ drawdowns.

**What is public vs proprietary.** Public: every threshold above, the funnel counts, the sector and group caps, the exit triggers, the holding period. Proprietary: the forensic model's ratios, the fair-value multiples, and how conviction maps to weight.

Sources: [SageOne India Growth GIFT Fund page](https://www.pmsaifworld.com/portfolio/sage-one-india-growth-gift-fund-gift-city-domiciled-category-iii-aif/) · [SageOne Flagship Growth OE Fund page](https://www.pmsaifworld.com/portfolio/sageone-flagship-growth-oe-fund/) · [SageOne Core Portfolio page](https://www.pmsaifworld.com/portfolio/sageone-core-portfolio/) · [Finalyca interview with Vartak](https://finalyca.com/blog/Sageone-Investment-Advisors-LLP/) · [Morningstar interview, Apr 2022](https://morningstar.in/posts/67749/samit-vartak-shares-his-multi-bagger-small-and-mid-cap-picks.aspx) · [PMS AIF World webinar notes, Jul 2021](https://www.pmsaifworld.com/blog/how-to-make-10x-returns-in-10years-samit-vartak-sageone-investment-managers-pms/) · [BloombergQuint on the 2021 memo](https://www.bloombergquint.com/markets/sage-ones-samir-vartak-on-the-kind-of-bull-market-were-in) · [SageOne AIF listing, Jun 2026](https://pmsbazaar.com/AMC/SageOne-Investment-Managers-LLP/AIF)

## Prashant Khemka / WhiteOak — OpcoFinco valuation inside a factor-neutral portfolio

Khemka is the only one here who is not really an X voice (WhiteOak's handles are corporate), but his framework is included because it is a distinct, named selection method that the ₹1,000-cr-plus WhiteOak AMC, PMS and the London-listed Ashoka trusts all run on. He built Goldman Sachs' India equity book from US$5 mn in 2007 to the GEM franchise, started in quant at State Street in 1998, and founded WhiteOak in June 2017.

**Paradigm.** Two beliefs. First, outsized returns come from great businesses at attractive valuations, where "great" means a company earning more than its cost of capital on incremental investment. Second, alpha should come only from stock selection, so the portfolio is deliberately balanced to neutralise macro, sector, currency and style bets, which in his view add volatility without adding return. Active managers in India have beaten the index after fees more persistently than anywhere else, which is his case for running the method here.

**Step 1 — Governance DNA screen.** Before any financial analysis, a structured screen of the promoter and board history removes companies with poor governance. Khemka's rule of thumb is that governance failures in emerging markets destroy more alpha than any valuation error, and the firm has a dedicated ESG research team to run this.

**Step 2 — Business-quality screen.** Three disqualifiers: weak incremental returns on capital, obsolescence risk from technology change, and businesses whose economics are not scalable. What survives is a company with a superior return on incremental capital and a long runway to deploy it. The India universe is about 1,300 companies and roughly 30 investment professionals cover it.

**Step 3 — OpcoFinco™ valuation.** This is the proprietary core. Every company is split in two:

- *Finco* owns all the invested capital (fixed assets, working capital) and is assumed to earn exactly the cost of capital on it.
- *Opco* is the capital-light operating business that earns everything above the cost of capital, that is, the excess return on invested capital (EROIC).

Mechanically: take enterprise value from the market, subtract invested capital, and what remains is the market's price for Opco. Divide that by the present value of the excess free cash flow Opco generates and you have the OpcoFinco FCF multiple. Companies are then compared on this multiple rather than on P/E or EV/EBITDA. The point is that two companies with the same P/E can have wildly different Opco multiples if one needs much more capital to grow; the framework prices the capital intensity explicitly. The fund documents say traditional methods are used alongside where appropriate.

**Step 4 — Balanced portfolio construction.** The stated objective is to maximise alpha while minimising the volatility of alpha. Residual factor exposures left over from stock picking (sector, size, momentum, currency) are measured continuously and contained so that no single macro factor can swamp stock-specific returns. Holdings are wide: 80–120 names in the multicap mutual fund, and the PMS books (Top 200, Pioneers) are also diversified rather than concentrated. Khemka has said in the Ashoka EM context that he will buy a non-EM listed stock (ASML, Budweiser APAC) purely because it mitigates an underweight factor risk, which shows how literal the balancing is.

**Step 5 — Review.** There is no published holding period or rebalance rule; positions are re-underwritten when the Opco multiple moves outside the range the team thinks is justified, and turnover is moderate. Khemka has publicly argued that managers who do not beat their benchmark should not be paid, and Ashoka's trusts charge only a performance fee.

**What this method is good and bad at.** Good: it penalises capital-hungry growth and rewards the capital-light franchise even at a high headline P/E, and the balanced construction means the track record is not a bet on one style (unlike Marcellus). Bad: with 80–120 names and factor neutralisation the alpha per name is small, so the result looks like an enhanced index; a PrimeInvestor review notes sector allocations do not deviate much from category averages.

**What is public vs proprietary.** Public: the Opco/Finco split, the FCF-multiple definition, the governance-first order of operations, the balanced-portfolio objective, the universe size and holding counts. Proprietary: the cost-of-capital assumptions, the excess-FCF forecasting, and the factor model used for balancing.

Sources: [Trustnet interview with Khemka, May 2024](https://www.trustnet.com/news/13414198/ashokas-khemka-managers-cant-just-sit-on-their-laurels-and-expect-to-get-paid) · [WhiteOak Quality Equity Fund draft, Angel One, Sep 2024](https://www.angelone.in/news/mutual-funds/whiteoak-capital-quality-equity-fund-sebi-draft-filed) · [WhiteOak Pioneers deck, Sep 2023](https://www.sharekhan.com/MediaGalary/Newsletter/White-Oak-India-Equity-Fund-VI-WOIEF-VI-Sep-2023-Product-Deck-280923.pdf) · [Finalyca on WhiteOak's four pillars](https://finalyca.com/blog/White-Oak-Capital-Management-Consultants-LLP) · [PrimeInvestor: Inside the house, WhiteOak AMC](https://primeinvestor.in/reports/inside-the-house-whiteoak-amc/) · [5paisa profile](https://5paisa.com/news/meet-the-experienced-stock-market-guru-prashant-khemka) · [Advisorkhoj interview](https://www.advisorkhoj.com/interviews/BFSI-Industry-Interview/It-is-all-about-having-a-very-well-resourced-team-which-can-identify-winners-within-the-midcap-space) · [AltPort GIFT fund page](https://www.altportfunds.com/?p=1936)

## Vijay Kedia — SMILE (X: @VijayKedia1)

Kedia is a proprietary investor, not a fund manager, so there is no deck; the method is reconstructed from two decades of interviews, his TEDx talk and his posts. He started at 14 in a Kolkata broking family, bought 5% of Aegis Logistics at ₹20 in 2004–05 as his first big position, and over 2004–2017 turned Aegis, Atul Auto and Cera Sanitaryware into three separate 100x outcomes. His disclosed portfolio was about ₹1,475 cr at Dec 2023 and ₹1,689 cr by mid-2024, held in roughly 15 stocks; Atul Auto, at about 21% of the company, is his largest holding and the clearest statement of how concentrated he is willing to be.

**Paradigm.** Buy a small, unknown company run by an experienced and hungry management in a market that is far bigger than the company, pay a fair rather than cheap price, and hold for 10–15 years without deciding the exit at entry. He has said he is 100% invested in equities at all times and has been for 30 years, and that he prefers stocks that will not move for six months after he buys because that signals no one else is watching. The whole method is a management filter first and a market-size filter second; there is no financial screen.

**S — Small in size.** Not small in market cap for its own sake, but small market share inside a large and growing industry, so the growth ahead is many multiples of what has been achieved. He explicitly avoids companies that are cheap because the management is weak: you cannot get a small company that is cheap and also has prime return ratios, so one of the three has to give, and he gives on price.

**M — Medium in experience.** Management that has run the same business for 10–20 years and lived through at least one full down-cycle. He has said that history alone is not enough; what he is checking is whether the promoter stayed in the core business when it was unfashionable. He meets promoters in person before buying and has described this as the only due diligence that cannot be outsourced.

**L — Large in aspiration.** The promoter must still have "fire in the belly" to go from small to medium and medium to large, rather than being content at the current size. He reads this from capacity-expansion decisions, hiring of professional management, and whether the promoter talks about the industry rather than the share price.

**E — Extra-large market potential.** Better to be a small fish in a big pond: the addressable market must have no near-term ceiling. His Cera call in 2004–05 was a bet that organised sanitaryware would take share from the unorganised segment for decades; the Atul Auto call was that three-wheelers were a turnaround sector and the management was unknown and therefore unpriced.

**What he does not do.** No P/E or ROCE threshold is ever stated; he has said he is willing to pay up for the right management. No stop loss on investments (he keeps a strict cut-loss rule only for his trading book, which he runs separately and says most people should not run at all). No exit decided at entry: his line is that the money is not his until he sells, so unrealised profit never tempts him out. He also tells retail investors to hold fewer stocks than a fund does precisely because they can afford to know each management.

**Position sizing and risk.** Concentration is the risk control: a position is sized to what he would be comfortable holding through a 50% drawdown, and he has repeatedly said the three things he checks are management, business growth and the downside. His 2018 advice on Alpha Moguls was to bet big and ride through tough times. He does not diversify for its own sake; the 15-odd names span auto ancillaries, telecom equipment, building materials, pharma and construction because each passed SMILE, not for sector balance.

**Current lens: RISE.** In 2025–26 he has added a thematic overlay he calls RISE (renewable energy, infrastructure, security, emerging themes) to steer where he looks for the next SMILE candidates. It is a sourcing filter, not a change to the selection method.

**On X.** His feed is aphorisms and market-mood commentary rather than analysis; the useful signal is his shareholding disclosures (Trendlyne, exchange filings) and the long-form interviews, not the posts. He is also prone to hyperbole and has had public spats about stock tips, so treat anything stock-specific on the feed as noise.

**What is public vs proprietary.** Public: the four SMILE tests, the management-first order, the no-exit-at-entry rule, the concentration. Private: how he actually scores a promoter meeting, and what price he will pay; there is no number anywhere.

Sources: [BloombergQuint Alpha Moguls, Mar 2018](https://www.bloombergquint.com/markets/this-dalal-street-veterans-secret-to-investing-is-smile) · [Outlook Money on SMILE and RISE](https://www.outlookmoney.com/invest/stock-specific-investing-is-key-to-wealth-creation-says-vijay-kedia-owner-of-kedia-securities) · [Business Standard interview, Oct 2012](https://www.business-standard.com/article/companies/no-academic-degree-can-guarantee-success-in-stock-mkts-vijay-kedia-112101100166_1.html) · [Value Research on SMILE](https://www.valueresearchonline.com/stories/54019/how-to-invest-like-vijay-kedia/) · [Sharpely on Kedia's philosophy](https://sharpely.in/blogs/vijay-kedia-investing-philosophy/) · [Business Today, Jan 2023](https://www.businesstoday.in/amp/markets/story/how-to-spot-multibaggers-in-stock-market-vijay-kedia-shared-his-100x-mantra-360558-2023-01-17) · [Angel One portfolio note, Feb 2024](https://www.angelone.in/news/share-market/unveiling-vijay-kedia-multibagger-picks-stocks-under-rs-500-revealed) · [5paisa holdings, 2024](https://5paisa.com/blog/vijay-kedia-portfolio-and-shareholding) · [Wikipedia](https://en.wikipedia.org/wiki/Vijay_Kedia)

## Basant Maheshwari — sector leader in a secular trend, bought at a premium (X: @BMTheEquityDesk)

Maheshwari's method is a sector-first growth model: find the industry that is compounding at 20–30%, buy its leader, pay the premium P/E, and hold until the trend itself fades. He has run it as a proprietary investor since the 1990s, through The Equity Desk forum (2006), the Basant's Corner advisory (2011–15) and a SEBI PMS since Feb 2016 (about ₹250–300 cr AUM; 3-year return 16.16% against 25.15% for BSE 500 TRI as of the Groww listing). His book, The Thoughtful Investor (2014), is the full statement of the method. Named hits: Pantaloon Retail (₹7 to a sale around ₹280 on the way to ₹875), Page Industries (bought at a P/E of 16 in Mar 2009 when blue chips were at 5–6x), TV18, Titan, Hawkins, Gruh Finance, Repco, Can Fin Homes, Bajaj Finance, Avenue Supermarts.

**Paradigm.** The most money is made by concentrated bets, and concentrated bets can only be made in secular growers, never in cyclicals. A company growing revenue and profit above the market rate will always trade at a premium P/E, so waiting for cheap means never owning it; the premium is the price of certainty. His test for whether to buy is not valuation but whether the growth is predictable and the sector has a tailwind.

**Step 1 — Find the sector.** Two price-based tells for an emerging mega-sector: a whole pack of stocks from one sector making new highs day after day, and the sector growing at about 30%. He also looks at global analogues (software in 1994, organised retail in the 2000s, housing finance in 2015) and at what is in sync with world trends. He does not screen the whole market bottom-up; the sector call comes first.

**Step 2 — Buy the leader, never the poorer cousin.** Within the sector, the company with the strongest cash flows and highest ROE. High ROE is treated as proof that growth is self-funding: a company that has doubled sales over five years without diluting equity passes; one that has diluted fails. Dividend-paying leaders are preferred because dividends are tax-advantaged, keep growing, and put a floor under the stock.

**Step 3 — Check management intent through three proxies.** Promoter shareholding (high and not being sold), dividend payout history, and accounting practices. Turnaround stories and ideas from chat forums are explicitly excluded.

**Step 4 — Scalability.** The business must be able to grow in both volume and value; niche or non-scalable businesses are rejected even if cheap. The BM Focused Small Cap smallcase (launched 2022) states the numbers he uses when he is forced to write them down: market leaders growing revenue at 20% with ROE above 20% at reasonable valuations, 15 stocks across consumer, technology, financials, capital goods, construction and real estate.

**Step 5 — Buy strength, hold through growth.** He is comfortable buying at 52-week highs because secular trends keep making highs. Holding period is "as long as profits are growing"; he mocks the instinct to sell at ₹150 what you bought at ₹100. His framing on sizing: if you cannot take ₹100 to ₹120, do not let it fall to ₹60, meaning position size should be set so a loss is survivable but the upside is still meaningful.

**Step 6 — Exit on the theme, not the return.** The sell signal is the growth story fading, never the percentage gained. Two price-based tells that a theme is ending: the sector's stocks stop holding new highs for more than a couple of days, and illiquid names in the pack start outrunning the leaders. When that happens he shifts the book toward higher-quality large caps until the next sector call.

**Portfolio rules.** Concentrated, typically 10–15 stocks, multicap with no size constraint (average PMS market cap about ₹13,000 cr in 2022). His financial-independence rule for individuals: a portfolio worth 50 times annual expenses, at which point a 1–2% dividend yield covers living costs.

**Where the method breaks.** Pantaloon is his own example of selling too early; the Pantaloon-to-Future Group collapse later showed the leader-in-a-trend thesis can fail on balance-sheet risk that the ROE screen does not catch. The PMS's underperformance since 2021 reflects the same exposure as Marcellus: paying a premium multiple for predictable growth works until multiples compress across the board.

**On X.** He posts often, mostly sector-mood calls, PMS performance updates and pushback at critics; he was early and loud on housing finance (2015) and on small-caps bottoming (Jun 2022). The feed is a good read on which sector he thinks is in a mega-trend, which is step 1 of his own method.

**What is public vs proprietary.** Public: every step above, the 20%/20% screen for the small-cap product, the 50x-expenses rule, the exit logic. Private: which sector he thinks is next until he says so, and how he sets the P/E he is willing to pay.

Sources: [Value Research: Finding multi-baggers the Basant Maheshwari way](https://www.valueresearchonline.com/stories/27492/finding-multi-baggers-the-basant-maheshwari-way/) · [CFA Society India: Thoughtful Investor book reading, May 2014](https://cfasocietyindia.org/book-reading-event-the-thoughtful-investor/) · [CFA Society India: Riding the Bull and Handling the Bear, Feb 2015](https://cfasocietyindia.org/riding-the-bull-and-handling-the-bear/) · [PMS AIF World: Basant Maheshwari PMS](https://www.pmsaifworld.com/portfolio/basant-maheshwari-pms/) · [smallcase: BM Focused Small Cap launch](https://www.smallcase.com/blog/newest-launch-from-basant-maheshwari-investing-bm-focused-small-cap-smallcase/) · [Groww PMS listing](https://groww.in/pms/basant-maheswari-equity-fund-basant-maheshwari-wealth-advisers-llp) · [The Quint interview](https://www.thequint.com/amp/story/videos/news-videos/basant-maheshwari-on-share-market)

## Side by side

The seven methods split cleanly into two families: the two momentum shops decide on price alone and turn the book over in weeks or months, while the five fundamentals-driven investors hold for years and differ mainly in how they define quality and whether they have a hard sell rule.

&#91;embedded content: seven methods placed by signal source and holding period\]

The lower-left pair never reads an annual report; the upper-right cluster never looks at a chart. Maheshwari sits in between because his sector call comes from price action and his stock pick from ROE and cash flow.

| Method | Universe | Core filter | Portfolio | Rebalance / sell rule | Known failure mode | Reproducible from public data? |
| --- | --- | --- | --- | --- | --- | --- |
| Capitalmind Adaptive Momentum | NSE, liquidity-filtered (\~top 500) | Volatility-adjusted composite momentum, top decile, entry filters against parabolic moves | 25–30, equal weight, sector caps | Monthly; cash when fewer than 30 qualify; breadth rule to exit | Lags index \~1 in 3 years; half of entries exit red; 2024–26 two-year underperformance | Largely: formula weights private, structure public |
| WeekendInvesting Mi series | Fixed index universes (Nifty 50, NNF 50, MidSmall 400, CNX 500, >₹1,000 cr) | Relative-strength rank, top N | 10–50, equal at entry, some reset weights semi-annually | Weekly; rotational always invested, absolute variants go to cash | High churn and tax; whipsaw in sideways markets | Yes, closely |
| Marcellus Consistent Compounders | \~1,000 listed, 6-yr financials | 10–12 forensic ratios → deciles; 10 consecutive years of double-digit growth and ROCE > cost of capital; moat research | 12–15 (CCP), 15–20 (Little Champs) | Near-zero churn; exit on forensic-score deterioration; since 2024, trim when price outruns 5-yr expected return | Paid any price for quality; 2022–24 regime change cost ₹4,000 cr of redemptions | Steps 1–2 yes (MeritorQ is that product); step 3 no |
| SageOne | \~2,500 → ranks 101–1,000, ₹5–10k cr sweet spot | Earnings doubling in 3–4 yrs; ROCE/ROE > 20%; CFO/PAT > 1; incremental ROCE rising; six red flags | 12–25, conviction-weighted; 25% sector cap, 20% group cap | 3–4 yr hold; five explicit exit triggers; 80–90% turnover in 5 yrs | Structural thesis that turns cyclical (Indo Count); small-cap impact cost | Screen yes; forensic model and fair value no |
| WhiteOak | \~1,300 Indian listed | Governance screen → incremental-ROIC screen → OpcoFinco FCF multiple | 80–120, factor-balanced | Re-underwrite on multiple drift; no stated cadence | Alpha per name is small; looks index-like | Framework yes; inputs no |
| Kedia SMILE | Small, unfollowed companies | Management tenure 10–20 yrs, aspiration, small share of a very large market; no financial threshold | \~15, highly concentrated (21% of Atul Auto) | 10–15 yr hold; no exit decided at entry; no stop loss | Promoter judgement error has no quantitative backstop | No, it is a judgement method |
| Maheshwari | Sector first, then its leader | Sector growing \~30% with stocks making new highs; leader by ROE and cash flow; no dilution in 5 yrs | 10–15, concentrated, multicap | Hold while profits grow; sell when the theme fades (stocks stop holding highs) | Premium multiples compress together; leader can carry balance-sheet risk (Pantaloon/Future) | Sector tells yes; leader choice partly |

If you want to code something tonight, the order of difficulty is: WeekendInvesting (an afternoon), Capitalmind (a weekend, the cash rule is the hard part), Marcellus steps 1–2 (needs six years of consolidated statements per company), SageOne's green/red flags (same data plus CFO and incremental ROCE), WhiteOak OpcoFinco (needs invested-capital and cost-of-capital assumptions per company). Kedia and Maheshwari are not codeable; their value is as checklists for a promoter meeting and a sector call.

## Handles and how to verify them

Impersonation of all seven is common on X, and SEBI has fined people for running tip channels in their names. Verify a handle against the firm's own website or disclosure document before trusting anything stock-specific; Marcellus and Zerodha publish official-handle lists for exactly this reason.

| Person | Handle | Firm handle | Where the method actually lives |
| --- | --- | --- | --- |
| Deepak Shenoy | @deepakshenoy | @capitalmind\_in | capitalmind.in strategy pages, podcast, SSRN paper |
| Prashanth Krishna (Capitalmind trading) | @Prashanth\_Krish | — | Live momentum-book observations |
| Alok Jain | @WeekendInvestng | — | smallcase factsheets, weekendinvesting.com, daily newsletter |
| Saurabh Mukherjea | @saurabhmukherjea (verify) | @MarcellusInvest | marcellus.in newsletters and product decks |
| Samit Vartak | @SamitVartak | — | SageOne quarterly memos (sageoneinvestments.com), PMS AIF World pages |
| Prashant Khemka | not personally active | WhiteOak corporate handles | SEBI scheme documents, Ashoka trust reports, Trustnet interview |
| Vijay Kedia | @VijayKedia1 | — | Exchange shareholding filings, long-form interviews |
| Basant Maheshwari | @BMTheEquityDesk | — | The Thoughtful Investor, PMS newsletters, CFA Society talks |

Every fact in this report was taken from a page opened on 23 Oct 2026 and linked at the end of its section. Performance figures are the managers' own and are not SEBI-verified unless stated. Where a step of a method is not published, the section says so rather than filling it in.

