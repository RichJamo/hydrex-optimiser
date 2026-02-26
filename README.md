# Hydrex Vote Optimizer

A production-ready Python tool for analyzing and optimizing voting returns on Hydrex DEX (Linea blockchain). Maximize your bribe earnings through data-driven vote allocation.

## 🎯 Overview

This tool helps Hydrex voters optimize their weekly vote allocation to maximize bribe returns. It:

- Indexes historical voting and bribe data from the Linea blockchain
- Analyzes past epochs to identify optimal strategies
- Monitors current epoch bribe accumulation in real-time
- Recommends optimal vote allocation using quadratic optimization
- Tracks your expected returns vs. naive strategies

## 🏗️ Architecture

```
hydrex-vote-optimizer/
├── config.py              # Configuration (RPC, addresses, epochs)
├── main.py                # CLI entry point
├── src/
│   ├── indexer.py         # Blockchain data fetching
│   ├── database.py        # SQLite storage layer
│   ├── bribe_tracker.py   # Track bribe deposits via events
│   ├── optimizer.py       # Vote allocation algorithms
│   ├── price_feed.py      # Token price lookups (CoinGecko)
│   └── utils.py           # Helper functions
└── analysis/
    ├── historical.py      # Analyze past epochs
    ├── live_monitor.py    # Real-time current epoch tracking
    └── recommender.py     # Generate vote recommendations
```

## 🚀 Quick Start

### Prerequisites

- Python 3.10+
- Linea RPC endpoint
- (Optional) CoinGecko API key for better rate limits

### Installation

```bash
# Clone the repository
git clone https://github.com/RichJamoPrompt/hydrex-vote-optimizer.git
cd hydrex-vote-optimizer

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your settings
```

### Configuration

Edit `.env` with your details:

```env
RPC_URL=https://rpc.linea.build
VOTER_ADDRESS=0x...                    # Hydrex VoterV5 contract
MY_ESCROW_ADDRESS=0x...                      # Your wallet address
YOUR_VOTING_POWER=1000000              # Your voting power (in wei)
COINGECKO_API_KEY=                     # Optional
DATABASE_PATH=data/db/data.db
```

### Initial Setup

```bash
# Initialize database and test connection
python main.py setup

# Backfill historical data (last 12 epochs)
python main.py backfill

# Analyze historical performance
python main.py historical
```

## 📊 Usage

### Get Vote Recommendation (Saturday Evening)

```bash
python main.py recommend
```

Output:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  OPTIMAL VOTE ALLOCATION - Epoch 145
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Pool: WETH/USDC (0xabc...)
  Vote: 450,000 (45.0%)
  Expected Return: $125.50

Pool: USDT/DAI (0xdef...)
  Vote: 300,000 (30.0%)
  Expected Return: $89.30

Pool: WBTC/WETH (0x123...)
  Vote: 250,000 (25.0%)
  Expected Return: $71.20

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total Expected Return: $286.00
Opportunity Cost vs Naive: +$42.30 (+17.3%)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⏰ Epoch ends in 3 days, 5 hours
```

### Live Monitoring

```bash
python main.py monitor
```

Continuously tracks current epoch bribe deposits and updates recommendations in real-time.

### Historical Analysis

```bash
python main.py historical
```

Analyzes past 12 epochs to show optimal vs. naive strategy performance.

## 🧮 Algorithms

### Quadratic Optimization

The optimizer maximizes total expected return:

```python
maximize: Σ (your_votes[i] / (current_votes[i] + your_votes[i])) × bribes_usd[i]
subject to: Σ your_votes[i] = your_voting_power
            your_votes[i] ≥ 0
```

Uses `scipy.optimize.minimize` with SLSQP method for constraint optimization.

### Expected Return Calculation

```python
def expected_return(gauge, your_votes, total_bribes_usd):
    total_votes = current_votes + your_votes
    your_share = your_votes / total_votes
    return total_bribes_usd × your_share
```

## 📅 Epoch Timing

- **Epoch Duration**: 7 days (604,800 seconds)
- **Epoch Flip**: Wednesday 00:00:00 UTC
- **Safe Voting Window**: Saturday 18:00 - Tuesday 20:00 UTC
- **Recommended Time**: Saturday evening (after bribe flow slows)

## 🔧 Advanced Usage

### Custom Analysis Window

```bash
python main.py historical --epochs 24  # Analyze last 24 epochs
```

### Export Recommendations

```bash
python main.py recommend --format json > votes.json
```

### Dry Run Backfill

```bash
python main.py backfill --dry-run  # Preview without storing
```

## 📦 Dependencies

- **web3.py**: Blockchain interaction
- **pandas/numpy**: Data analysis
- **scipy**: Optimization algorithms
- **SQLAlchemy**: Database ORM
- **requests**: HTTP client for price feeds
- **python-dotenv**: Environment configuration
- **rich**: Beautiful terminal output
- **click**: CLI framework

## 🛡️ Important Notes

### Voting Constraints

- Can only vote once per epoch (VoteDelay check)
- Cannot vote during epoch flip (block.timestamp == epochTimestamp)
- Cannot vote in stale epoch (> epochTimestamp + DURATION)
- Votes apply to CURRENT epoch
- Bribes claimed after epoch ends

### Rate Limiting

- CoinGecko free tier: 10-30 calls/minute
- RPC rate limits vary by provider
- Built-in caching and retry logic

## 🤝 Contributing

Contributions welcome! Please:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Follow code style (type hints, docstrings, error handling)
4. Add tests for new functionality
5. Commit changes (`git commit -m 'Add amazing feature'`)
6. Push to branch (`git push origin feature/amazing-feature`)
7. Open a Pull Request

### Code Style

- Type hints on all functions
- Docstrings (Google style)
- Error handling with logging
- Max line length: 100 characters
- Use `black` for formatting
- Use `mypy` for type checking

## 📝 License

MIT License - see LICENSE file for details

## ⚠️ Disclaimer

This tool is for informational purposes only. Always verify recommendations and understand the risks before voting. Past performance does not guarantee future results.

## 🆘 Support

- Issues: https://github.com/RichJamoPrompt/hydrex-vote-optimizer/issues
- Discussions: https://github.com/RichJamoPrompt/hydrex-vote-optimizer/discussions

## 🗺️ Roadmap

- [ ] Multi-epoch lookahead optimization
- [ ] Machine learning for bribe prediction
- [ ] Telegram/Discord bot integration
- [ ] Web dashboard
- [ ] Automated voting execution
- [ ] Multi-wallet support
- [ ] Gas cost optimization

---

**Made with ❤️ for the Hydrex community**
