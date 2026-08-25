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


## Functionality

- **StockService**: This service fetches stock data from an external API and parses it into a usable format.
- **Utility Functions**: Helper functions for formatting currency values and calculating year-over-year growth.
- **Data Types**: Defined structures for stock-related data to ensure consistency throughout the application.

## Contributing

Contributions are welcome! Please submit a pull request or open an issue for any enhancements or bug fixes.

## License

This project is licensed under the MIT License. See the LICENSE file for more details.
