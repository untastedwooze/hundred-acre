"""Minimal reference sync: one Security per share-class FIGI, plus the Tickers that map to it."""

from sqlalchemy import Engine, text

from hundred_acre.market.source import MarketDataSource


def sync_reference(source: MarketDataSource, engine: Engine) -> None:
    """Create a Security for each exchange-listed share-class FIGI not yet known, and map Tickers to them.

    Over-the-counter Tickers and Tickers without a FIGI are outside the Universe and skipped.
    """
    with engine.begin() as conn:
        for rec in source.tickers():
            if rec.market == "otc" or not rec.share_class_figi:
                continue
            security_id = conn.execute(
                text(
                    "INSERT INTO securities (share_class_figi, name, type) VALUES (:figi, :name, :type) "
                    "ON CONFLICT (share_class_figi) DO UPDATE SET name = EXCLUDED.name, type = EXCLUDED.type "
                    "RETURNING security_id"
                ),
                {"figi": rec.share_class_figi, "name": rec.name, "type": rec.type},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO tickers (ticker, security_id) VALUES (:t, :s) "
                    "ON CONFLICT (ticker) DO UPDATE SET security_id = EXCLUDED.security_id"
                ),
                {"t": rec.ticker, "s": security_id},
            )
