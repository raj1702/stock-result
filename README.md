# Stock Results Skill

This project is designed to fetch and process stock results, including key financial metrics such as Price-to-Earnings (PE) ratio, Price/Earnings to Growth (PEG) ratio, profit margin, year-over-year (YOY) revenue growth, and year-over-year profit growth.

## Project Structure

```
stock-results-skill
├── src
│   ├── app.py                # Entry point of the application
│   ├── services
│   │   └── stock_service.py  # Service for fetching and parsing stock data
│   ├── utils
│   │   └── helpers.py        # Utility functions for data formatting and calculations
│   └── types
│       └── __init__.py       # Data types and interfaces for stock data
├── requirements.txt          # Project dependencies
├── .gitignore                # Files and directories to ignore in version control
└── README.md                 # Project documentation
```

## Installation

To set up the project, clone the repository and install the required dependencies:

```bash
git clone <repository-url>
cd stock-results-skill
pip install -r requirements.txt
```

Set an Upstox access token before starting the app. It is used for the
fundamentals fallback and the recommendation card's historical closing prices:

```bash
export UPSTOX_ACCESS_TOKEN="your-access-token"
```

## Usage

To run the application, execute the following command:

```bash
python src/app.py
```

Open `http://127.0.0.1:5050/` in your browser.

## Admin plan and quota management

The private `scripts/manage_user_plan.py` command can grant a subscription
tier, reset a user's quota, or assign a custom quota. It writes directly to
the configured DynamoDB table, so run it only from a trusted administrator
machine or role.

Before using it:

1. Confirm that `.env` contains `AWS_REGION` and `DYNAMODB_TABLE_NAME`.
2. Authenticate the local AWS CLI with `aws login`.
3. Obtain the user's Cognito `sub` from
   `http://localhost:5050/auth/status` while signed in as that user. Use the
   `sub`, not the email address.

The command is a dry run unless `--apply` is included. Always preview an
operation first and verify the user ID, table, region, and requested change.

### Grant Silver for 30 days

Preview:

```bash
python3 scripts/manage_user_plan.py \
  --user-sub "COGNITO_SUB" \
  --tier silver \
  --reason "Beta tester reward"
```

Apply after checking the preview:

```bash
python3 scripts/manage_user_plan.py \
  --user-sub "COGNITO_SUB" \
  --tier silver \
  --reason "Beta tester reward" \
  --apply
```

### Reset quota without changing the tier

```bash
python3 scripts/manage_user_plan.py \
  --user-sub "COGNITO_SUB" \
  --reset-quota \
  --reason "Customer support quota reset" \
  --apply
```

### Assign a custom quota

```bash
python3 scripts/manage_user_plan.py \
  --user-sub "COGNITO_SUB" \
  --quota 100 \
  --reason "Special beta allowance" \
  --apply
```

### Grant Gold for 60 days

```bash
python3 scripts/manage_user_plan.py \
  --user-sub "COGNITO_SUB" \
  --tier gold \
  --days 60 \
  --reason "Extended beta access" \
  --apply
```

Every tier grant, quota override, or quota reset begins a fresh quota cycle.
Old usage records remain in DynamoDB for audit purposes. Applied administrator
actions also create an `ADMIN#...` audit record against the user. Normal
Paid upgrades and automatic tier downgrades remove custom quota overrides.

Do not expose this command through a public web route or place AWS access keys
in `.env`. EC2 and ECS deployments should use the restricted IAM role described
in `docs/aws-production-auth.md`.


## Functionality

- **StockService**: This service fetches stock data from an external API and parses it into a usable format.
- **Utility Functions**: Helper functions for formatting currency values and calculating year-over-year growth.
- **Data Types**: Defined structures for stock-related data to ensure consistency throughout the application.

## Contributing

Contributions are welcome! Please submit a pull request or open an issue for any enhancements or bug fixes.

## License

This project is licensed under the MIT License. See the LICENSE file for more details.
