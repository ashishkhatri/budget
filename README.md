# Budget

A personal budgeting app that runs on your own computer. It imports bank statements (CSV or PDF), categorizes the transactions, and stores everything in a local SQLite database.

## Running it

Double-click **start.bat**. On the first run it creates a private Python environment (`.venv`) and installs two packages, Flask and pdfplumber, which takes about a minute. After that it opens the app in your browser at http://127.0.0.1:8765.

Keep the black console window open while you use the app. Closing it stops the app.

## Where your data lives

| Path | What |
|---|---|
| `data\budget.sqlite` | The database. Every transaction, account, rule and budget is here. |
| `data\backups\` | Automatic daily backups (the newest 14 are kept), plus any you make from the app. A backup is also made before you delete an account or wipe your data. |

To restore a backup, close the app, copy the backup file over `data\budget.sqlite`, and start the app again.

You can open the database in any SQLite tool, such as [DB Browser for SQLite](https://sqlitebrowser.org/), or from Python with `sqlite3`. Amounts are stored as integer cents in `amount_cents`. Negative amounts are money out and positive amounts are money in.

## Supported imports

- **CSV**: Chase (checking and credit card), Bank of America, Wells Fargo, Capital One, American Express, Discover and Citi are detected automatically. Other banks' CSVs are handled by guessing which columns hold the Date, Description and Amount (or Debit and Credit).
- **PDF**: a best-effort text reader. It doesn't work on scanned image PDFs. Prefer CSV when your bank offers it.

You can import overlapping date ranges. Transactions that are already in the database are skipped.

**Possible duplicates.** That check only catches exact copies in the same account: the same date, amount and description. A **Possible duplicates** panel on the Transactions page catches what gets past it:
- the same charge read slightly differently by two files, like a CSV and a PDF of the same month;
- a posting date in one file and the purchase date in the other;
- one statement imported into two accounts (e.g. "Chase Credit Card" and "Chase Credit Card 9743").

A pair is flagged when all of these hold:
- the same amount;
- nearly the same description, with no conflicting store numbers;
- dates at most 4 days apart;
- different imports.

Some things never count as duplicates:
- **Rows from the same file:** two $5 coffees on one statement are two coffees.
- **Matching charges on two different cards:** these are left alone unless it's the same file imported twice.

For each pair you can **Delete** the extra one or mark them **Not duplicates**. The suggested *keep* is the one you've categorized or noted, otherwise the one imported first. **Delete all extras** makes a backup first.

## Adding transactions by hand

**+ Add** on the Transactions page records anything your bank files don't have, such as cash purchases.
- **Fields:** type the merchant, amount, date, category and account. The account defaults to a **Cash** account, which is created the first time.
- **📷 Scan a receipt:** the image model in Settings → Local AI reads the store, date, total and main items from a photo, and fills in the form for you to check. The image model needs to be a vision model, like Qwen-VL or Gemma 3. "Same as text model" works when your text model can read images.
- **Already there?** If a transaction with the same amount is already there (from your bank, within a few days), the app offers to add the receipt's items to it as a note instead of adding a copy.

## Merchants

Every transaction belongs to a **merchant**. Merchants are their own records: they keep their name, category and notes even if you delete all their transactions. The bank's description is cleaned up into a short name (e.g. `0252-GAYLORD TEXAN GRAPVINE TX` becomes `GAYLORD TEXAN`), and that name is linked to a merchant.

On the **Merchants** page you can:
- **Set a category:** every transaction from that merchant, past and future, goes there. This includes ones you changed one by one; you can still change a single transaction afterwards as an exception. "Automatic" lets rules, keywords and AI decide again.
- **Rename** a merchant, e.g. `GAYLORD TEXAN` to "Gaylord Texan Resort".
- **Merge** merchants the bank spells differently, e.g. `AMAZON.COM` and `AMAZON MKTPL`. The bank names become alternate names, so future imports go to the merged merchant.
- **Split** a merchant that lumps several businesses together. The short name is the first two words of the description, so `CLEAR LAKE HUNAN RESTAURA`, `CLEAR LAKE WATER` and `CLEAR LAKE REG MED CAFE` all become `CLEAR LAKE`.
  - **Split…** appears when a merchant's descriptions differ. It groups the transactions by their longer name; a city added at the end doesn't count as a different business.
  - Give each group its merchant: a new name, or an existing merchant's name to move it there.
  - Future descriptions containing that group's name go to the same merchant.
- **Delete** a merchant that has no transactions.

## Recurring

The **Recurring** tab finds subscriptions, bills and memberships on its own; there's nothing to set up. It looks at each merchant's charges and calls them recurring when the gaps between them match one of these:

## How categorizing works (in order)

1. **A category you set on one transaction**: one-off exceptions.
2. **A category you set on its merchant.**
3. **Your rules**: "description contains X → category", on the Categories & Rules page.
4. **Built-in merchant list**: in `budget\keywords.py`, which you can edit.
5. **The bank's own category**, when the CSV includes one.
6. **The AI's answer for that merchant**: it's saved on the merchant, so each merchant is asked about only once.
7. **Uncategorized**: these appear in the Needs review panel on the Transactions page.

Categories have a *kind*. **Spending** categories count toward spending, **Income** categories count as income, and **Transfer** categories are ignored in both totals. The Transfer kind is how credit-card payments avoid being counted twice.

## Local AI categorization (LM Studio)

Anything still **Uncategorized** after the steps above can be categorized by a model running in LM Studio on your computer. Nothing leaves your machine.

1. In LM Studio, load a model and start the local server: **Developer** tab → **Start Server** (default port 1234).
2. In the app, open **Settings → Local AI**. Click **Connect** and pick a model (or leave it on "First loaded model"). Settings save automatically.
3. **When importing:** the preview asks the AI about uncategorized rows as soon as a file is loaded, and shows its suggestions with an **AI** tag before you click Import. Turn this off with "Suggest categories in the import preview automatically"; the preview then shows an **✦ AI-categorize** button instead.
4. **For transactions already imported:** click **✦ Categorize with AI** on the dashboard or the Transactions page.

**Reasoning ("thinking") models:** picking a category needs no long deliberation, and a thinking model can spend thousands of tokens per batch. LM Studio ignores the API settings for turning thinking off, so:
- **Qwen models** are asked with thinking switched off: the app writes the prompt itself with an empty `<think></think>` block and sends it to LM Studio's `/v1/completions`. If that doesn't work for a model, it falls back to normal chat.
- **Other models** may think for about 1,000 tokens plus 150 per merchant in the batch. Past that the request is stopped with a message. To turn thinking off for such a model in LM Studio: **My Models** → the model → **Inference** → **Prompt Template**, add `{%- set enable_thinking = false %}` as the first line, and reload it. Or pick a model without thinking.

Any OpenAI-compatible server works, including Ollama at `http://127.0.0.1:11434/v1`.

## Bank sync (Plaid)

**Settings → Bank connections** downloads transactions straight from your bank through [Plaid](https://plaid.com), instead of importing files. Plaid's free *Trial* plan connects up to 10 banks, including Chase.

**Setup:**
1. Sign up at [dashboard.plaid.com](https://dashboard.plaid.com/signup).
2. Copy your **client_id** and **secret** from Developers → Keys and paste them into the app. Try **Sandbox** first if you like: log in with `user_good` / `pass_good`.
3. Click **Connect a bank**. You sign in to the bank in Plaid's window; for Chase, on chase.com.

**Security:**
- **Your password:** you type it in Plaid's or the bank's window, never in this app.
- **Read-only:** the app only asks for the *transactions* product. It never asks for account/routing numbers or payment features, so nothing here can move money.
- **Encrypted storage:** your Plaid secret and the connection tokens are encrypted with your Windows login (DPAPI). A copy of the database or a backup can't use them on another computer or Windows account.
- **What Plaid sees:** Plaid, a third party, sees your transactions. That's the trade-off compared with file imports, where nothing leaves your computer.
- **Revoking access:** at any time in Chase's settings (Security → Linked apps), at [my.plaid.com](https://my.plaid.com), or with **Disconnect** here.

**How syncing works:**
- **When it runs:** when the app starts, and whenever you click **Sync now**. Each sync fetches only what changed since the last one.
- **Pending charges** are skipped until they post.
- **Accounts** are matched by their last 4 digits ("Chase Credit Card 9743"), so synced and imported transactions share one account.
- **No doubles:** a synced transaction that matches one you imported (same account, same amount, dates within 3 days) is linked instead of added. That includes ones in a catch-all account with no card number in its name.
- **Older history:** history from before an account's first imported transaction is skipped, assuming it's already in your files.

## Planned: Amazon purchase matching (on hold)

**Goal:** stop Amazon charges showing up as one vague "Shopping" lump. Import your Amazon order history, match each Amazon card charge to the items in it, and categorize by what was actually bought, using the local AI on item names.
**Open decision:** when one charge covers items in different categories, either split the transaction (needs split-transaction support, which would also help with Costco and Target) or use the category of the most expensive item.
**Next step:** request the "Your Orders" export, then build the importer against its real columns.

## Adding a bank API later

`budget/providers/base.py` defines a `Provider` with two methods: `list_accounts()` and `fetch_transactions()`. The second returns `Candidate` objects that carry a stable `external_id`. `sync_account()` sends them through the same pipeline as file imports, so rules, categories and dedupe all work unchanged. The database already has `accounts.source`/`external_id` and `transactions.external_id`/`pending` columns for this. Re-syncing overlapping dates is safe because `(account_id, external_id)` is unique.

To change the database schema, add a new numbered entry to `MIGRATIONS` in `db.py`. Don't edit the existing entries.

## Releasing

Bump `__version__` in `budget/__init__.py` and push to `main`. That's all.
- **What happens:** GitHub Actions sees a version with no release yet. It runs the tests and creates the `v<version>` tag and release, with the commit message as the release notes.
- **Pushes that don't change the version** only run the tests.
