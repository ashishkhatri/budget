# Changelog

Each version here becomes a GitHub release automatically when it reaches `main`
(see `.github/workflows/release.yml`). To release: add a section at the top and bump
`__version__` in `budget/__init__.py`.

## v0.8.0

- **Accounts & Data is now called Settings**, in the menu, the page title and messages that point to it.

This is also the first release on GitHub. What the app does so far:
- **Import** CSV and PDF statements (Chase and other major banks). Files and transactions you've already imported are detected and skipped.
- **Merchants** are records of their own: rename, merge, split, set a category for all their transactions, and see each one's typical charge.
- **Categorizing** uses your rules, built-in keywords, the bank's category, and optionally a local AI model in LM Studio.
- **Dashboard** for any period, with clickable income, spending and categories.
- **Budgets.**
- **Transactions** with multi-select filters, a Needs review panel and a Possible duplicates panel.
- **Recurring:** subscriptions and bills weekly to yearly, including amounts that change.
- **Bank sync** through Plaid: optional and read-only, with keys encrypted with your Windows login.
