"""
Database module for storing and querying Hydrex data.
Uses SQLite with SQLAlchemy ORM.
"""

import logging
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    Integer,
    String,
    create_engine,
    desc,
    text,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

Base = declarative_base()


class Epoch(Base):
    """Epoch information."""

    __tablename__ = "epochs"

    timestamp = Column(Integer, primary_key=True)
    total_votes = Column(Integer, default=0)
    total_bribes_usd = Column(Float, default=0.0)
    indexed_at = Column(Integer)  # When data was indexed

    def __repr__(self) -> str:
        return f"<Epoch(timestamp={self.timestamp}, total_votes={self.total_votes})>"


class Gauge(Base):
    """Gauge information."""

    __tablename__ = "gauges"

    address = Column(String, primary_key=True)
    pool = Column(String)
    internal_bribe = Column(String)
    external_bribe = Column(String)
    is_alive = Column(Boolean, default=True)
    created_at = Column(Integer)

    def __repr__(self) -> str:
        return f"<Gauge(address={self.address}, pool={self.pool})>"


class Vote(Base):
    """Vote records per epoch per gauge."""

    __tablename__ = "votes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    epoch = Column(Integer, index=True)
    gauge = Column(String, index=True)
    total_votes = Column(Float)  # Changed from Integer to handle large blockchain values
    indexed_at = Column(Integer)

    def __repr__(self) -> str:
        return f"<Vote(epoch={self.epoch}, gauge={self.gauge}, votes={self.total_votes})>"


class Bribe(Base):
    """Rewards (trading fees + bribes) for voters per epoch per gauge.
    
    Tracks RewardAdded events from both internal (fees) and external (incentives) 
    bribe contracts attached to each gauge.
    """

    __tablename__ = "bribes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    epoch = Column(Integer, index=True)
    bribe_contract = Column(String, index=True)  # Internal or external bribe contract
    reward_token = Column(String)  # Token being offered as reward
    amount = Column(Float)  # Amount of reward tokens (legacy, human units)
    amount_wei = Column(String)  # Raw amount in smallest unit (string to avoid overflow)
    timestamp = Column(Integer)
    indexed_at = Column(Integer)

    def __repr__(self) -> str:
        return f"<Bribe(epoch={self.epoch}, contract={self.bribe_contract[:10]}..., amount={self.amount})>"


class HistoricalAnalysis(Base):
    """Historical analysis results."""

    __tablename__ = "historical_analysis"

    epoch = Column(Integer, primary_key=True)
    optimal_return = Column(Float)
    naive_return = Column(Float)
    opportunity_cost = Column(Float)
    optimal_allocation = Column(String)  # JSON string
    analyzed_at = Column(Integer)

    def __repr__(self) -> str:
        return f"<HistoricalAnalysis(epoch={self.epoch}, optimal=${self.optimal_return})>"


class TokenPrice(Base):
    """Cached token prices."""

    __tablename__ = "token_prices"

    token_address = Column(String, primary_key=True)
    usd_price = Column(Float)
    updated_at = Column(Integer)  # Timestamp when price was fetched

    def __repr__(self) -> str:
        return f"<TokenPrice(token={self.token_address[:10]}..., price=${self.usd_price})>"


class HistoricalTokenPrice(Base):
    """Historical token prices (per timestamp + granularity)."""

    __tablename__ = "historical_token_prices"

    token_address = Column(String, primary_key=True)
    timestamp = Column(Integer, primary_key=True)  # period start unix
    granularity = Column(String, primary_key=True)  # "hour" or "day"
    usd_price = Column(Float)
    updated_at = Column(Integer)

    def __repr__(self) -> str:
        return (
            f"<HistoricalTokenPrice(token={self.token_address[:10]}..., "
            f"ts={self.timestamp}, granularity={self.granularity}, price=${self.usd_price})>"
        )


class TokenMetadata(Base):
    """Cached token metadata (symbol, decimals)."""

    __tablename__ = "token_metadata"

    token_address = Column(String, primary_key=True)
    symbol = Column(String)
    decimals = Column(Integer)
    updated_at = Column(Integer)

    def __repr__(self) -> str:
        return (
            f"<TokenMetadata(token={self.token_address[:10]}..., "
            f"symbol={self.symbol}, decimals={self.decimals})>"
        )


class Database:
    """Database interface for Hydrex data."""

    def __init__(self, db_path: str):
        """
        Initialize database connection.

        Args:
            db_path: Path to SQLite database file
        """
        self.engine = create_engine(f"sqlite:///{db_path}")
        self.Session = sessionmaker(bind=self.engine)
        logger.info(f"Database initialized: {db_path}")

    def create_tables(self) -> None:
        """Create all tables if they don't exist."""
        Base.metadata.create_all(self.engine)
        self._ensure_bribes_amount_wei_column()
        logger.info("Database tables created")

    def _ensure_bribes_amount_wei_column(self) -> None:
        """Ensure the bribes table has an amount_wei column (migration helper)."""
        with self.engine.begin() as conn:
            result = conn.execute(text("PRAGMA table_info(bribes)"))
            columns = [row[1] for row in result.fetchall()]
            if "amount_wei" not in columns:
                conn.execute(text("ALTER TABLE bribes ADD COLUMN amount_wei TEXT"))

    def get_session(self) -> Session:
        """Get a new database session."""
        return self.Session()

    # Epoch operations
    def save_epoch(
        self, timestamp: int, total_votes: int = 0, total_bribes_usd: float = 0.0
    ) -> None:
        """Save or update epoch information."""
        with self.get_session() as session:
            epoch = session.query(Epoch).filter_by(timestamp=timestamp).first()
            if epoch:
                epoch.total_votes = total_votes
                epoch.total_bribes_usd = total_bribes_usd
            else:
                epoch = Epoch(
                    timestamp=timestamp,
                    total_votes=total_votes,
                    total_bribes_usd=total_bribes_usd,
                    indexed_at=int(datetime.utcnow().timestamp()),
                )
                session.add(epoch)
            session.commit()
            logger.debug(f"Saved epoch {timestamp}")

    def get_epoch(self, timestamp: int) -> Optional[Epoch]:
        """Get epoch by timestamp."""
        with self.get_session() as session:
            return session.query(Epoch).filter_by(timestamp=timestamp).first()

    def get_recent_epochs(self, count: int = 12) -> List[Epoch]:
        """Get most recent epochs."""
        with self.get_session() as session:
            return (
                session.query(Epoch)
                .order_by(desc(Epoch.timestamp))
                .limit(count)
                .all()
            )

    # Historical token price operations
    def get_historical_token_prices(
        self, token_addresses: List[str], timestamp: int, granularity: str
    ) -> dict:
        """Get historical token prices for a timestamp and granularity."""
        if not token_addresses:
            return {}
        with self.get_session() as session:
            rows = (
                session.query(HistoricalTokenPrice)
                .filter(
                    HistoricalTokenPrice.token_address.in_(token_addresses),
                    HistoricalTokenPrice.timestamp == timestamp,
                    HistoricalTokenPrice.granularity == granularity,
                )
                .all()
            )
            return {row.token_address.lower(): row.usd_price for row in rows}

    def save_historical_token_prices(
        self, prices: List[tuple[str, int, str, float]]
    ) -> None:
        """Save historical token prices.

        Args:
            prices: List of (token_address, timestamp, granularity, usd_price)
        """
        if not prices:
            return
        now_ts = int(datetime.utcnow().timestamp())
        with self.get_session() as session:
            for token_address, timestamp, granularity, usd_price in prices:
                existing = (
                    session.query(HistoricalTokenPrice)
                    .filter_by(
                        token_address=token_address.lower(),
                        timestamp=timestamp,
                        granularity=granularity,
                    )
                    .first()
                )
                if existing:
                    existing.usd_price = usd_price
                    existing.updated_at = now_ts
                else:
                    session.add(
                        HistoricalTokenPrice(
                            token_address=token_address.lower(),
                            timestamp=timestamp,
                            granularity=granularity,
                            usd_price=usd_price,
                            updated_at=now_ts,
                        )
                    )
            session.commit()

    # Gauge operations
    def save_gauge(
        self,
        address: str,
        pool: str,
        internal_bribe: str,
        external_bribe: str,
        is_alive: bool = True,
    ) -> None:
        """Save or update gauge information."""
        with self.get_session() as session:
            gauge = session.query(Gauge).filter_by(address=address).first()
            if gauge:
                gauge.pool = pool
                gauge.internal_bribe = internal_bribe
                gauge.external_bribe = external_bribe
                gauge.is_alive = is_alive
            else:
                gauge = Gauge(
                    address=address,
                    pool=pool,
                    internal_bribe=internal_bribe,
                    external_bribe=external_bribe,
                    is_alive=is_alive,
                    created_at=int(datetime.utcnow().timestamp()),
                )
                session.add(gauge)
            session.commit()
            logger.debug(f"Saved gauge {address}")

    def get_gauge(self, address: str) -> Optional[Gauge]:
        """Get gauge by address."""
        with self.get_session() as session:
            return session.query(Gauge).filter_by(address=address).first()

    def get_all_gauges(self, alive_only: bool = True) -> List[Gauge]:
        """Get all gauges."""
        with self.get_session() as session:
            query = session.query(Gauge)
            if alive_only:
                query = query.filter_by(is_alive=True)
            return query.all()

    def get_gauge_pool_address(self, gauge_address: str) -> Optional[str]:
        """Get pool address for a gauge from DB cache."""
        with self.get_session() as session:
            gauge = session.query(Gauge).filter_by(address=gauge_address).first()
            if gauge:
                return gauge.pool
            return None

    def save_gauge_pool_address(self, gauge_address: str, pool_address: str) -> None:
        """Save pool address for a gauge."""
        with self.get_session() as session:
            gauge = session.query(Gauge).filter_by(address=gauge_address).first()
            if gauge:
                gauge.pool = pool_address
                session.commit()
                logger.debug(f"Saved pool {pool_address} for gauge {gauge_address}")

    # Vote operations
    def save_vote(self, epoch: int, gauge: str, total_votes: int) -> None:
        """Save vote data for an epoch and gauge."""
        with self.get_session() as session:
            vote = (
                session.query(Vote)
                .filter_by(epoch=epoch, gauge=gauge)
                .first()
            )
            if vote:
                vote.total_votes = total_votes
                vote.indexed_at = int(datetime.utcnow().timestamp())
            else:
                vote = Vote(
                    epoch=epoch,
                    gauge=gauge,
                    total_votes=total_votes,
                    indexed_at=int(datetime.utcnow().timestamp()),
                )
                session.add(vote)
            session.commit()
            logger.debug(f"Saved vote for epoch {epoch}, gauge {gauge}")

    def get_votes_for_epoch(self, epoch: int) -> List[Vote]:
        """Get all votes for a specific epoch."""
        with self.get_session() as session:
            return session.query(Vote).filter_by(epoch=epoch).all()

    # Bribe operations
    def save_bribe(
        self,
        epoch: int,
        bribe_contract: str,
        reward_token: str,
        amount_wei: str,
        timestamp: int,
        amount: Optional[float] = None,
    ) -> None:
        """Save bribe/reward data from RewardAdded event."""
        with self.get_session() as session:
            bribe = Bribe(
                epoch=epoch,
                bribe_contract=bribe_contract,
                reward_token=reward_token,
                amount=amount,
                amount_wei=amount_wei,
                timestamp=timestamp,
                indexed_at=int(datetime.utcnow().timestamp()),
            )
            session.add(bribe)
            session.commit()
            logger.debug(f"Saved bribe for epoch {epoch}, contract {bribe_contract[:10]}...")

    def save_token_metadata(
        self,
        token_address: str,
        symbol: Optional[str] = None,
        decimals: Optional[int] = None,
    ) -> None:
        """Save or update token metadata (symbol, decimals)."""
        with self.get_session() as session:
            record = session.query(TokenMetadata).filter_by(token_address=token_address.lower()).first()
            if record:
                if symbol is not None:
                    record.symbol = symbol
                if decimals is not None:
                    record.decimals = decimals
                record.updated_at = int(datetime.utcnow().timestamp())
            else:
                record = TokenMetadata(
                    token_address=token_address.lower(),
                    symbol=symbol,
                    decimals=decimals,
                    updated_at=int(datetime.utcnow().timestamp()),
                )
                session.add(record)
            session.commit()

    def get_token_metadata(self, token_address: str) -> Optional[TokenMetadata]:
        """Get token metadata by address."""
        with self.get_session() as session:
            return (
                session.query(TokenMetadata)
                .filter_by(token_address=token_address.lower())
                .first()
            )

    def get_bribes_for_epoch(self, epoch: int) -> List[Bribe]:
        """Get all bribes for a specific epoch."""
        with self.get_session() as session:
            return session.query(Bribe).filter_by(epoch=epoch).all()

    def get_bribes_by_gauge(self, epoch: int, gauge_address: str) -> List[Bribe]:
        """Get bribes for a specific gauge in an epoch (checks both internal/external bribe contracts)."""
        with self.get_session() as session:
            # Get gauge to find its bribe contracts
            gauge = session.query(Gauge).filter_by(address=gauge_address).first()
            if not gauge:
                return []
            
            return (
                session.query(Bribe)
                .filter(
                    Bribe.epoch == epoch,
                    Bribe.bribe_contract.in_([gauge.internal_bribe, gauge.external_bribe])
                )
                .all()
            )

    # Historical analysis operations
    def save_analysis(
        self,
        epoch: int,
        optimal_return: float,
        naive_return: float,
        opportunity_cost: float,
        optimal_allocation: str,
    ) -> None:
        """Save historical analysis results."""
        with self.get_session() as session:
            analysis = (
                session.query(HistoricalAnalysis)
                .filter_by(epoch=epoch)
                .first()
            )
            if analysis:
                analysis.optimal_return = optimal_return
                analysis.naive_return = naive_return
                analysis.opportunity_cost = opportunity_cost
                analysis.optimal_allocation = optimal_allocation
                analysis.analyzed_at = int(datetime.utcnow().timestamp())
            else:
                analysis = HistoricalAnalysis(
                    epoch=epoch,
                    optimal_return=optimal_return,
                    naive_return=naive_return,
                    opportunity_cost=opportunity_cost,
                    optimal_allocation=optimal_allocation,
                    analyzed_at=int(datetime.utcnow().timestamp()),
                )
                session.add(analysis)
            session.commit()
            logger.debug(f"Saved analysis for epoch {epoch}")

    # Token price caching operations
    def save_token_price(self, token_address: str, usd_price: float) -> None:
        """Save or update token price in cache."""
        with self.get_session() as session:
            price_entry = session.query(TokenPrice).filter_by(token_address=token_address.lower()).first()
            if price_entry:
                price_entry.usd_price = usd_price
                price_entry.updated_at = int(datetime.utcnow().timestamp())
            else:
                price_entry = TokenPrice(
                    token_address=token_address.lower(),
                    usd_price=usd_price,
                    updated_at=int(datetime.utcnow().timestamp()),
                )
                session.add(price_entry)
            session.commit()

    def get_token_price(self, token_address: str, max_age_seconds: int = 3600) -> Optional[float]:
        """Get cached token price if recent enough."""
        with self.get_session() as session:
            price_entry = session.query(TokenPrice).filter_by(token_address=token_address.lower()).first()
            if price_entry:
                age = int(datetime.utcnow().timestamp()) - price_entry.updated_at
                if age < max_age_seconds:
                    return price_entry.usd_price
        return None

    def get_batch_token_prices(self, token_addresses: list[str], max_age_seconds: int = 3600) -> dict[str, float]:
        """Get multiple cached token prices."""
        with self.get_session() as session:
            current_time = int(datetime.utcnow().timestamp())
            prices = {}
            for addr in token_addresses:
                price_entry = session.query(TokenPrice).filter_by(token_address=addr.lower()).first()
                if price_entry:
                    age = current_time - price_entry.updated_at
                    if age < max_age_seconds:
                        prices[addr.lower()] = price_entry.usd_price
            return prices
