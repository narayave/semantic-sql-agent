# Snowflake setup

## Why Snowflake

The local DuckDB warehouse is a controlled lab: I planted every trap, so I
already know the answers. Running the same agent against a real cloud
warehouse is how I find out which parts of it actually hold up.

What the move teaches that the local setup hides:

- **Guardrails move.** DuckDB can open a database read only. Snowflake can't,
  so the real boundary has to be a role that can only `SELECT`. The regex in
  `run_sql` stays as the first check, not the last.
- **Real platform quirks.** Identifiers fold to uppercase, a session needs
  role, warehouse, database and schema all set, and a suspended warehouse adds
  seconds of latency. None of that exists locally.
- **Data I didn't design.** On a dataset with traps nobody planted, the
  semantic layer has to be written by reading the data, not by remembering
  what I put in it. Expect the eval score to drop. That drop is the result.
- **Same home as the product it models.** Snowflake Cortex Analyst answers
  questions from a YAML semantic model over Snowflake tables. Running here
  makes the comparison concrete instead of hand-wavy.

The agent loop, traces and eval harness don't change. Only the toolbox does:
`agent/snowflake_tools.py` returns the same results as `agent/tools.py`.

Setup takes about fifteen minutes. Nothing here needs writing, only filling in.

## 1. Trial account

signup.snowflake.com, 30 days free, no card. Pick Enterprise and any cloud.

From the account URL `https://xy12345.us-east-1.snowflakecomputing.com`, your
`SNOWFLAKE_ACCOUNT` is `xy12345.us-east-1`. Getting this wrong is the most
common first failure.

## 2. Fill in .env

```
SNOWFLAKE_ACCOUNT=xy12345.us-east-1
SNOWFLAKE_USER=your_user
SNOWFLAKE_PASSWORD=...
SNOWFLAKE_ROLE=ACCOUNTADMIN
SNOWFLAKE_WAREHOUSE=COMPUTE_WH
SNOWFLAKE_DATABASE=SNOWFLAKE_SAMPLE_DATA
SNOWFLAKE_SCHEMA=TPCH_SF1
```

`SNOWFLAKE_SAMPLE_DATA.TPCH_SF1` ships with every account: customers, orders,
lineitems, suppliers, nations, regions. Good enough to prove the plumbing.

## 3. Verify

```bash
pip install -r requirements.txt
python -c "
from agent.snowflake_tools import SnowflakeToolbox
tb = SnowflakeToolbox()
print(tb.list_tables().text)
print(tb.get_schema('CUSTOMER').text)
print(tb.run_sql('SELECT COUNT(*) AS n FROM customer').text)
"
```

Three passing calls means the port is done.

## 4. Chat UI

```bash
BACKEND=duckdb streamlit run app.py     # works now, no credentials
streamlit run app.py                    # against Snowflake
```

## Pointing it at a new dataset

1. Set `SNOWFLAKE_DATABASE` and `SNOWFLAKE_SCHEMA` to the new dataset
2. Run `list_tables` and `get_schema` on everything, read it properly
3. **Write the semantic layer first.** Find the terms that are not columns and
   define them. This matters more than any prompt change
4. Write golden cases with ground-truth SQL, including one that is
   deliberately unanswerable
5. Run the evals, report accuracy **and the failure buckets**

## Three things that will bite

**Identifier case.** Snowflake folds unquoted names to uppercase. `get_schema`
already compares with `UPPER()`. If the dataset was created with quoted
lowercase names, you will need quoted identifiers in SQL too.

**Session context.** Role, warehouse, database and schema are four separate
things. Missing one gives "Cannot perform SELECT. This session does not have a
current database", which looks like a permissions problem and is not.

**Warehouse suspend.** A trial warehouse auto-suspends. The first query after
idle takes a few seconds to resume. Not a bug.
