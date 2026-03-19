# Feature creation

`python -m features.calendar_creator`

To get dates from OHCLV and NSE trading holidays to create base set of trading dates.
> Note: removal logic for dates affected by ad hoc NSE announcements is not implemented.

```
public.dim_trading_days
  - date: timestamp with time zone NOT NULL
  - is_next_day_working: boolean
  - is_previous_day_working: boolean
  - is_month_end: boolean
  - is_quarter_end: boolean
  - is_year_end: boolean
  - week_number: bigint
  # Indexes
    dim_trading_days_date_key: UNIQUE (date)
    idx_dim_trading_days_date: UNIQUE (date)
```
