from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True, slots=True)
class PromptDefinition:
    name: str
    prompt: str
    is_table: bool
    query: str


REPORT_PROMPTS: Dict[str, PromptDefinition] = {
    "BalanceSheetAssets": PromptDefinition(
        name="BalanceSheetAssets",
        prompt="""**Goal:** Summarize the provided financial document and **include data for *all* reported years** using standardized financial metrics.
**Primary Data Source:** The CSV data must be treated as the authoritative source, while the OCR text should be used only to supplement or confirm any missing or unclear values.
---

### **Key Instructions**

1. **Identify and Include All Timeframes**
- The CSV contains multiple columns, each representing a Balance sheet as on a given date.
- **Extract the financial metrics for every year** in the CSV, not just one.
2. **Standardized Financial Metrics**
- Collect all line items (assets, liability and equity) **for each year**.
- Ensure each metric is matched to the correct month and year from the CSV.
3. **Currency and Units**
- Identify the currency (e.g., "Lakhs of rupees" or "Rs. in Lakhs") and **apply that unit consistently** to the extracted values.
4. **JSON Output Requirements**
- Use the following guidelines when structuring your JSON:
1. **Include an entry (or a nested object) for each given year** found in the CSV.
2. For each period, record the relevant metrics.
3. If a metric is not provided in the CSV or the text, set its value to `null`.

5. **Completeness Check**
- Double-check all rows/columns in the CSV to ensure no metrics are missed for any period.""",
        is_table=True,
        query="consol AND balan AND sheet AND asset AND current",
    ),
    "BalanceSheetEquity": PromptDefinition(
        name="BalanceSheetEquity",
        prompt="""**Goal:** Summarize the provided financial document and **include data for *all* reported years** using standardized financial metrics.
**Primary Data Source:** The CSV data must be treated as the authoritative source, while the OCR text should be used only to supplement or confirm any missing or unclear values.
---

### **Key Instructions**

1. **Identify and Include All Timeframes**
- The CSV contains multiple columns, each representing a Balance sheet as on a given date.
- **Extract the financial metrics for every year** in the CSV, not just one.
2. **Standardized Financial Metrics**
- Collect all line items (assets, liability and equity) **for each year**.
- Ensure each metric is matched to the correct month and year from the CSV.
3. **Currency and Units**
- Identify the currency (e.g., "Lakhs of rupees" or "Rs. in Lakhs") and **apply that unit consistently** to the extracted values.
4. **JSON Output Requirements**
- Use the following guidelines when structuring your JSON:
1. **Include an entry (or a nested object) for each given year** found in the CSV.
2. For each period, record the relevant metrics.
3. If a metric is not provided in the CSV or the text, set its value to `null`.

5. **Completeness Check**
- Double-check all rows/columns in the CSV to ensure no metrics are missed for any period.""",
        is_table=True,
        query="consol AND balan AND sheet AND equity AND share",
    ),
    "BalanceSheetLiabilities": PromptDefinition(
        name="BalanceSheetLiabilities",
        prompt="""**Goal:** Summarize the provided financial document and **include data for *all* reported years** using standardized financial metrics.
**Primary Data Source:** The CSV data must be treated as the authoritative source, while the OCR text should be used only to supplement or confirm any missing or unclear values.
---

### **Key Instructions**

1. **Identify and Include All Timeframes**
- The CSV contains multiple columns, each representing a Balance sheet as on a given date.
- **Extract the financial metrics for every year** in the CSV, not just one.
- In cases where the CSV has inconsistent or missing values, consult the OCR text to confirm or fill the gaps.
2. **Standardized Financial Metrics**
- Collect all line items (assets, liability and equity) **for each year**.
- Ensure each metric is matched to the correct month and year from the CSV.
3. **Currency and Units**
- Identify the currency (e.g., "Lakhs of rupees" or "Rs. in Lakhs") and **apply that unit consistently** to the extracted values.
4. **JSON Output Requirements**
- Use the following guidelines when structuring your JSON:
1. **Include an entry (or a nested object) for each given year** found in the CSV.
2. For each period, record the relevant metrics.
3. If a metric is not provided in the CSV or the text, set its value to `null`.

5. **Completeness Check**
- Double-check all rows/columns in the CSV to ensure no metrics are missed for any period.""",
        is_table=True,
        query="consol AND balan AND sheet AND liab AND current",
    ),
    "Pnl": PromptDefinition(
        name="Pnl",
        prompt="""**Goal:** Summarize the provided financial document and **include data for *all* reported periods** (e.g., multiple quarters or annual periods) using standardized financial metrics.
**Primary Data Source:** The CSV data must be treated as the authoritative source, while the OCR text should be used only to supplement or confirm any missing or unclear values.
---

### **Key Instructions**

1. **Identify and Include All Timeframes**
- The CSV contains multiple columns, each representing a separate reporting period (e.g., Quarter ended on June 2023, Year ended on March 2024, Half year ended on December 2023 etc.).
- **Extract the financial metrics for every column/period** in the CSV, not just one.
2. **Financial Timeframe Classification**
- Determine the type of each period based on the dates provided (e.g., "quarter", "half-yearly", or "annual").
- If the CSV or text specifically labels a period (e.g., "Quarter ended 30-Jun-24" or "Year ended 31-Mar-24"), use that label directly.
3. **Standardized Financial Metrics**
- Collect all line items (revenue, expenses, tax, profit etc., and break ups for them) **for each period**.
- Ensure each metric is matched to the correct period from the CSV.
4. **Data Accuracy & Consolidation**
- **Prioritize consolidated financial figures**:
- If the CSV already labels the statements as "Consolidated", use those numbers.
- Ignore or discard any non-consolidated data points.
- Avoid duplication: if the same data appears in both the text and the CSV, use the CSV version unless it is obviously incomplete or incorrect.
5. **Currency and Units**
- Identify the currency (e.g., "Lakhs of rupees" or "Rs. in Lakhs") and **apply that unit consistently** to the extracted values.
6. **JSON Output Requirements**
- Use the following guidelines when structuring your JSON:
1. **Include an entry (or a nested object) for each period** found in the CSV (e.g. Quarter ended on June 2023, Year ended on March 2024, Half year ended on December 2023 etc.).
2. For each period, record the relevant metrics.
3. If a metric is not provided in the CSV or the text, set its value to `null`.
7. **Completeness Check**
- Double-check all rows/columns in the CSV to ensure no metrics are missed for any period.""",
        is_table=True,
        query="consol AND (profit OR loss OR PAT OR EBITDA) AND expen AND reven",
    ),
    "COMMON": PromptDefinition(name="COMMON", prompt="", is_table=False, query=""),
    "ArbitrationAward": PromptDefinition(
        name="ArbitrationAward",
        prompt="""### **Goal**:
Extract information related to **arbitrations** that the company is involved in from **analyst calls or financial documents**. The arbitration cases may involve:
- Disputes with clients or other companies.
- Amounts stuck in arbitration.
- Provisions set aside for potential payments or receivables related to arbitration.

### **Guidelines for Extraction**:
- **Do not make any guesses.** If any information is unclear or not explicitly mentioned in the document, leave the corresponding fields **blank**.
- Capture the data **exactly as stated** in the document, preserving numeric values and context.
- Ensure accuracy in financial figures, including the **unit of rupees** mentioned in the document. If the unit is not provided, default to **"in Rs."**""",
        is_table=False,
        query="",
    ),
    "CashFlow": PromptDefinition(
        name="CashFlow",
        prompt="""**Goal:** Summarize the provided financial document and **include data for *all* reported periods** (e.g. multiple quarters or annual periods) using standardized financial metrics.
**Primary Data Source:** The CSV data must be treated as the authoritative source, while the OCR text should be used only to supplement or confirm any missing or unclear values.
---

### **Key Instructions**

1. **Identify and Include All Timeframes**
- The CSV contains multiple columns, each representing a separate reporting period (e.g., Quarter ended on June 2023, Year ended on March 2024, Half year ended on December 2023 etc.).
- **Extract the financial metrics for every column/period** in the CSV, not just one.
2. **Financial Timeframe Classification**
- Determine the type of each period based on the dates provided (e.g., "quarter", "half-yearly", or "annual").
- If the CSV or text specifically labels a period (e.g., "Quarter ended 30-Jun-24" or "Year ended 31-Mar-24"), use that label directly.
3. **Standardized Financial Metrics**
- Collect all line items (cash received, interest expenses, inventory etc., and break ups) **for each period**.
- Ensure each metric is matched to the correct period from the CSV.
4. **Data Accuracy & Consolidation**
- **Prioritize consolidated financial figures**:
- If the CSV already labels the statements as "Consolidated", use those numbers.
- Ignore or discard any non-consolidated data points.
5. **Currency and Units**
- Identify the currency (e.g., "Lakhs of rupees" or "Rs. in Lakhs") and **apply that unit consistently** to the extracted values.
6. **JSON Output Requirements**
- Use the following guidelines when structuring your JSON:
1. **Include an entry (or a nested object) for each period** found in the CSV (e.g. Quarter ended on June 2023, Year ended on March 2024, Half year ended on December 2023 etc.).
2. For each period, record the relevant metrics.
3. If a metric is not provided in the CSV or the text, set its value to `null`.
7. **Completeness Check**
- Double-check all rows/columns in the CSV to ensure no metrics are missed for any period.""",
        is_table=True,
        query="consol AND cash AND flow and expen AND income",
    ),
    "DebtCapital": PromptDefinition(
        name="DebtCapital",
        prompt="""### **Goal**:
Extract structured **debt capital details** from the provided **conference call transcript** or **exchange announcement**. The extracted data should include:
- **Debt Payoff**: Expected loan repayment and its impact on interest costs.
- **Long-Term Borrowing (LTB)**: Loan amount, finance cost, interest rate, and reason.
- **Short-Term Borrowing (STB)**: Loan amount, interest rate, and reason for borrowing.
- **Working Capital (WC)**: Any changes, amount required, reason, and interest costs.

### **Guidelines for Extraction**:
1. **Extract only explicitly mentioned information** – if a field is missing, leave it **blank**.
2. **Ensure accuracy** in numerical values, rupee units, and interest rates.
3. If the **unit of rupees** is **not provided**, default to **"Rs."**
4. Maintain the structured format as per the provided JSON schema.""",
        is_table=False,
        query="",
    ),
    "EmployeeCount": PromptDefinition(name="EmployeeCount", prompt="", is_table=False, query=""),
    "FundRaise": PromptDefinition(
        name="FundRaise",
        prompt="""### **Goal**:
Extract structured **fund raise details** from the provided **conference call transcript** or **stock exchange announcement**. The extracted data should include:
- **Amount**: Total amount of funds being raised.
- **Key Parties**: Institutions, funds, or individuals involved in the fundraising.
- **Fund Raise Type**: Preferential allotment, QIP, Rights Issue, or other modes.
- **Promoter Participation**: Whether the company's promoters are participating.
- **Timeline**: Expected time for completion in months.
- **Future Plans**: Whether the company plans to raise funds in the future.

### **Guidelines for Extraction**:
1. **Extract only explicitly mentioned information** – if a field is missing, leave it **blank**.
2. **Ensure accuracy** in numerical values, rupee units, and timeframes.
3. If the **unit of rupees** is **not provided**, default to **"Rs."**
4. Maintain the structured format as per the provided JSON schema.""",
        is_table=False,
        query="",
    ),
    "Guidance": PromptDefinition(
        name="Guidance",
        prompt="""### **Goal**:
Extract structured **financial and business performance data** from the following **earnings call transcript** according to the provided JSON schema. Ensure that the extracted data accurately captures:
- **Orders**: Current and expected orders for future periods.
- **Revenue**: Expected revenue growth and revenue derived from capacity expansion.
- **Margins**: EBITDA, PAT, and operating margins across different time periods.
- **Capacity Expansion**: Status, expected timeline, utilization, and revenue impact.

### **Guidelines for Extraction**:
- **No assumptions or guesses.** If any value is **not explicitly stated**, leave the corresponding field **blank**.
- Capture **numerical values accurately**, including percentages and currency units.
- Extract trends (e.g., flat, increase, decrease) **only if explicitly mentioned** in the transcript.
- Maintain **precision** in financial figures, and ensure the **unit of rupees** is accurately reflected. If not specified, **default to "Rs."**""",
        is_table=False,
        query="__all__",
    ),
    "OrderBook": PromptDefinition(
        name="OrderBook",
        prompt="""### **Goal**:
Extract structured **order book details** from the provided **order book update announcement** or **earnings call transcript**. The extracted data should include:
- **Order Breakdown**: Individual orders, their descriptions, and execution timelines.
- **Financial Data**: Order values in the correct **unit of rupees** as mentioned in the document.
- **Execution Timeline**: Estimated time (in months) for order fulfillment.

### **Guidelines for Extraction**:
1. **Do not make any assumptions** – extract only explicitly stated information. If any data point is missing, leave it **blank**.
2. **Preserve accuracy** in numerical values, currency units, and execution timelines.
3. If the **unit of rupees** is **not provided**, default to **"Rs."**
4. Ensure the extracted data follows the **JSON schema** provided below.""",
        is_table=False,
        query="",
    ),
}


CATEGORY_PROMPTS: Dict[str, str] = {}
