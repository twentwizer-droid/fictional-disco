"""THE ONLY place Axiom URLs are built. No referral code, no shortener, no redirect.

Format taken from a real Axiom share link:
  https://axiom.trade/meme/{address}?chain=sol
Only Solana is verified. Whether {address} is the mint or the pool is checked by probe.py.
"""
import logging

log = logging.getLogger("links")

CHAIN = {"solana": "sol"}
HOME_URL = "https://axiom.trade"


def coin_url(address: str, chain: str = "solana") -> str:
    if chain not in CHAIN or not address:
        log.warning("coin_url: unsupported chain %r or empty address, returning HOME_URL", chain)
        return HOME_URL
    return f"https://axiom.trade/meme/{address}?chain={CHAIN[chain]}"
